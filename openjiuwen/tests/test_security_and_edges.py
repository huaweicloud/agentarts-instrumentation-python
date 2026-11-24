# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Security / edge / reliability tests (P0/P1 gaps from the test-lead review matrix).

Four dimensions (design.md §8):
- Security: S1 reject CRLF injection in headers (validated at config layer),
  S2 non-empty endpoint, S3 alert logs never leak span content;
- Edge: B1 Windows backslash skill path, B2 aggregator LRU eviction,
  B3 malformed input.messages, B4 Unicode truncate, B5 large batches,
  B6 post-shutdown semantics;
- Reliability: R1 concurrent exports, R2 recovery after consecutive inner
  failures;
- Functional: F1 negative config-validation matrix, F2 host native OTLP
  conflict yield (probe must not inject when the host exporter=otlp_* and
  its endpoint points at the AgentArts gateway).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from agentarts_instrumentation.config import ExporterConfig, Projection
from agentarts_instrumentation.exporter.buffer import BufferedOtlpExporter
from agentarts_instrumentation.openjiuwen.inject import (
    ENV_APM_ENABLE,
    ENV_RESOURCE_ID,
    ENV_TOKEN,
    _host_otlp_conflict,
    _load_settings,
    _read_env_file,
    main,
)
from agentarts_instrumentation.openjiuwen.preset import _TurnInputAggregator
from agentarts_instrumentation.projection import project_span
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class CollectingExporter(SpanExporter):
    """Fake inner exporter that records exported spans."""

    def __init__(self) -> None:
        self.exported: list = []
        self.calls = 0

    def export(self, spans) -> SpanExportResult:
        self.calls += 1
        self.exported.extend(list(spans))
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


class FlakyExporter(CollectingExporter):
    """export() always raises while fail_enabled=True (explicitly turned off
    mid-test to simulate recovery).

    No fixed failure count: under pytest, span construction is slower than
    the background consumer draining items one by one, so batch partitioning
    and the failure window race; a toggle keeps assertions independent of
    batch boundaries.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail_enabled = True
        self.attempts = 0
        self.failures = 0

    def export(self, spans) -> SpanExportResult:
        self.attempts += 1
        if self.fail_enabled:
            self.failures += 1
            raise RuntimeError("simulated inner failure")
        return super().export(spans)


class SlowExporter(CollectingExporter):
    """export() blocks for delay seconds -- for force_flush timeout semantics."""

    def __init__(self, delay: float = 2.0) -> None:
        super().__init__()
        self._delay = delay

    def export(self, spans) -> SpanExportResult:
        time.sleep(self._delay)
        return super().export(spans)


class FailingResultExporter(CollectingExporter):
    """export() raises nothing but returns FAILURE -- how the official OTLP
    exporter actually behaves on network failure."""

    def export(self, spans) -> SpanExportResult:
        return SpanExportResult.FAILURE


def _make_config(**overrides: Any) -> ExporterConfig:
    params: dict[str, Any] = {
        "endpoint": "http://localhost:4318/v1/traces",
        "flush_interval_ms": 50,
    }
    params.update(overrides)
    return ExporterConfig(**params)


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---- security ----


def test_s1_headers_crlf_injection_rejected():
    """Header keys/values containing control chars (CR/LF/NUL) must be
    rejected at the config layer."""
    for headers in (
        {"authentication": "token\r\nX-Injected: 1"},
        {"authentication": "token\nX-Injected: 1"},
        {"x-api-key": "k\x00v"},
        {"bad\r\nkey": "value"},
    ):
        with pytest.raises(Exception, match="control characters"):
            ExporterConfig(endpoint="http://localhost:4318/v1/traces", headers=headers)


def test_s1_headers_legal_unicode_accepted():
    """Legal non-ASCII header values (e.g. accented Latin) pass the
    control-char validation untouched."""
    cfg = ExporterConfig(
        endpoint="http://localhost:4318/v1/traces",
        headers={"authentication": "tökén-vàlüe"},
    )
    assert cfg.headers == {"authentication": "tökén-vàlüe"}


def test_s2_empty_endpoint_rejected():
    with pytest.raises(Exception):
        ExporterConfig(endpoint="", protocol="http")


def test_s3_drop_warning_logs_never_contain_span_attributes():
    """Queue-full drop warnings contain only counts, never span attribute
    values (sensitive content / credentials must not reach logs).

    Captured by attaching a handler directly (independent of caplog's root
    propagation; most robust in multi-logger environments).
    """

    class _Captor(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    target = logging.getLogger("agentarts_instrumentation.exporter.buffer")
    captor = _Captor()
    target.addHandler(captor)
    try:
        # Slow inner (1s block) guarantees the main loop hits queue_size=2
        # first: the drop warning fires deterministically.
        inner = SlowExporter(delay=1.0)
        exporter = BufferedOtlpExporter(inner, _make_config(queue_size=2))
        for index in range(10):
            exporter.export([_span_with_secret(f"span-{index}")])
        exporter.force_flush(timeout_millis=3000)
        exporter.shutdown()
    finally:
        target.removeHandler(captor)

    assert any("drop" in message for message in captor.messages)
    for message in captor.messages:
        assert "SECRET-TOKEN-XYZ" not in message, message
    # Pin down the required log contract: N drops produce only the first
    # warning + a flush/shutdown summary, never one log line per drop.
    drop_logs = [m for m in captor.messages if "drop" in m or "full" in m]
    assert exporter._dropped >= 1
    assert len(drop_logs) <= 4, drop_logs
    summary = [m for m in drop_logs if "summary" in m]
    assert any(f"{exporter._dropped} total dropped" in m for m in summary), drop_logs


def _span_with_secret(name: str):
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, TraceFlags

    return ReadableSpan(
        name=name,
        context=SpanContext(
            trace_id=0x0102030405060708090A0B0C0D0E0F10,
            span_id=0x1122334455667788,
            is_remote=False,
            trace_flags=TraceFlags.SAMPLED,
        ),
        parent=None,
        resource=None,
        attributes={"gen_ai.output.messages": "SECRET-TOKEN-XYZ"},
        events=[],
        links=[],
        kind=None,
        status=None,
        start_time=1,
        end_time=2,
        instrumentation_scope=None,
    )


# ---- functional: F1 negative config-validation matrix ----


@pytest.mark.parametrize(
    "overrides",
    [
        {"queue_size": 0},
        {"batch_size": 0},
        {"flush_interval_ms": 0},
        {"export_timeout_ms": 99},
        {"protocol": "tcp"},
    ],
)
def test_f1_config_validation_rejects_invalid(overrides):
    with pytest.raises(Exception):
        _make_config(**overrides)


# ---- functional: F2 yield to host native OTLP ----


def _host_obs_config(exporter: str, endpoint: str) -> Any:
    """Minimal stand-in for the host ObservabilityConfig (the probe reads it
    leniently via getattr)."""
    return SimpleNamespace(exporter=exporter, endpoint=endpoint)


def test_f2_conflict_agentarts_gateway_detected():
    """Host native OTLP (otlp_grpc/otlp_http) pointing at the AgentArts
    gateway must be detected as a conflict.

    Covers three endpoint shapes: https, bare gRPC host:port, and
    myhuaweicloud.com domains (root-cause scenario of duplicated spans
    observed on a colleague's setup, 2026-11-20).
    """
    endpoints = (
        "https://apm-access.cn-southwest-2.myhuaweicloud.com",
        "apm-access.cn-southwest-2.myhuaweicloud.com:4317",
        "https://access.cn-north-4.myhuaweicloud.com:4317",
    )
    for endpoint in endpoints:
        for exporter in ("otlp_grpc", "otlp_http"):
            conflict = _host_otlp_conflict(_host_obs_config(exporter, endpoint))
            assert conflict is not None, (exporter, endpoint)
            assert exporter in conflict
            assert endpoint in conflict


def test_f2_no_conflict_local_or_unrelated_targets():
    """Local collector / third-party collector / non-OTLP exporter / empty
    endpoint: not blocked."""
    cases = [
        # Default local jiuwenswarm shape: local Langfuse path (nothing listening)
        ("otlp_http", "http://localhost:3101/api/public/otel/v1/traces"),
        ("otlp_grpc", "http://127.0.0.1:4317"),
        ("otlp_grpc", "localhost:4317"),
        # Third-party collector: the client cannot determine the forwarding
        # target (documented in README)
        ("otlp_grpc", "http://collector.example.com:4317"),
        # Endpoint unset (host runtime defaults to localhost)
        ("otlp_grpc", ""),
        # Non-OTLP exporters: unrelated to AgentArts reporting
        ("file", "https://apm-access.cn-southwest-2.myhuaweicloud.com"),
        ("console", "https://apm-access.cn-southwest-2.myhuaweicloud.com"),
        ("langfuse", "https://apm-access.cn-southwest-2.myhuaweicloud.com"),
    ]
    for exporter, endpoint in cases:
        assert _host_otlp_conflict(_host_obs_config(exporter, endpoint)) is None, (
            exporter,
            endpoint,
        )


def test_f2_tolerates_config_shape_drift():
    """On config shape drift (missing fields / None) it safely returns None
    and never breaks host initialization."""
    assert _host_otlp_conflict(SimpleNamespace()) is None
    assert _host_otlp_conflict(None) is None


# ---- functional: F3 official access variables (agentarts_agentops_*, aligned 2026-11-20) ----


def test_f3_env_file_parser_shape(tmp_path):
    """Simple .env parsing: KV lines, comments, blank lines, duplicate keys
    (first wins), key/value stripping, BOM tolerance."""
    env_file = tmp_path / "agentarts.env"
    env_file.write_bytes(
        (
            "# official access variables\n"
            "agentarts_agentops_apm_token=tok-official\n"
            "\n"
            "agent_resource_id=rid-1\n"
            "agent_resource_id=rid-2\n"
            "resource_type = agent \n"
        ).encode("utf-8-sig")
    )
    settings = _read_env_file(str(env_file))
    assert settings["agentarts_agentops_apm_token"] == "tok-official"
    assert settings["agent_resource_id"] == "rid-1"  # duplicate key: first wins
    assert settings["resource_type"] == "agent"  # key and value are stripped
    assert ENV_APM_ENABLE not in settings  # comments/blank lines yield no keys


def test_f3_load_settings_env_over_file(tmp_path, monkeypatch):
    """Process env vars take precedence over the env file; legacy 0.1.x
    variable names load from the file too."""
    env_file = tmp_path / "agentarts.env"
    env_file.write_text(
        "agentarts_agentops_apm_token=from-file\n"
        "AGENTARTS_AGENT_ID=legacy-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTARTS_ENV_FILE", str(env_file))
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv("AGENTARTS_TRACE_TOKEN", raising=False)
    settings = _load_settings()
    assert settings[ENV_TOKEN] == "from-file"
    assert settings["AGENTARTS_AGENT_ID"] == "legacy-file"
    # Process env var overrides the same-named file value
    monkeypatch.setenv(ENV_TOKEN, "from-env")
    assert _load_settings()[ENV_TOKEN] == "from-env"


def test_f3_main_skips_when_apm_disabled(monkeypatch):
    """apm_enable explicitly false: skip even with full credentials, and
    register no injection hook."""
    import sys as _sys

    monkeypatch.setenv(ENV_APM_ENABLE, "false")
    monkeypatch.setenv(ENV_TOKEN, "tok")
    monkeypatch.setenv(ENV_RESOURCE_ID, "rid")
    before = len(_sys.meta_path)
    assert main() is False
    assert len(_sys.meta_path) == before


def test_f3_main_skips_without_credentials(monkeypatch):
    """All of official/legacy token and resource_id missing: skip, register
    no injection hook."""
    import sys as _sys

    monkeypatch.setenv("AGENTARTS_ENV_FILE", "")
    for name in (
        ENV_TOKEN,
        "AGENTARTS_TRACE_TOKEN",
        ENV_RESOURCE_ID,
        "AGENTARTS_AGENT_ID",
        ENV_APM_ENABLE,
    ):
        monkeypatch.delenv(name, raising=False)
    before = len(_sys.meta_path)
    assert main() is False
    assert len(_sys.meta_path) == before


# ---- edge ----


def test_b1_windows_backslash_skill_path(make_span):
    """SKILL.md reads from a Windows backslash path must be recognized
    (pins down the primary deployment shape)."""
    span = make_span(
        "execute_tool read_file",
        attributes={
            "gen_ai.tool.name": "read_file",
            "gen_ai.tool.call.arguments": json.dumps(
                {"path": ".agents\\skills\\jw-coding-flow\\SKILL.md"}
            ),
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "jw-coding-flow"
    assert any(event.name == "skill.loaded" for event in out.events)


def test_b2_aggregator_lru_eviction():
    aggregator = _TurnInputAggregator(capacity=2)
    aggregator.record(1, "a")
    aggregator.record(2, "b")
    aggregator.record(3, "c")  # evicts the oldest (trace 1)
    assert aggregator.take(1) is None
    assert aggregator.take(2) == "b"
    assert aggregator.take(3) == "c"
    assert aggregator.take(3) is None  # take consumes idempotently


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        json.dumps([{"role": "system", "content": "no user message"}]),
        json.dumps([{"role": "user", "parts": [{"type": "text"}]}]),
        json.dumps([{"role": "user", "content": ""}]),
        "",
    ],
)
def test_b3_malformed_input_messages_never_crash_or_fill(make_span, raw):
    """Malformed input.messages: never raises, and never fabricates input
    for AGENT spans."""
    llm = make_span(
        "chat model",
        attributes={"gen_ai.operation.name": "chat", "gen_ai.input.messages": raw},
    )
    project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.s",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert "gen_ai.input.messages" not in out.attributes


def test_b4_unicode_truncate_counts_characters(make_span):
    span = make_span(attributes={"note": "😀" * 100, "text": "ünïcödé-tëxt" * 50})
    out = project_span(span, Projection(truncate=5))
    assert len(out.attributes["note"]) == 5  # counts characters, not bytes
    assert len(out.attributes["text"]) == 5


def test_b5_large_batch_single_export(make_span):
    """1000 spans in a single export() all enqueue (zero loss within capacity)."""
    inner = CollectingExporter()
    exporter = BufferedOtlpExporter(inner, _make_config(queue_size=4096))
    spans = [make_span(name=f"bulk-{i}") for i in range(1000)]
    assert exporter.export(spans) == SpanExportResult.SUCCESS
    assert exporter.force_flush(timeout_millis=5000)
    assert len(inner.exported) == 1000
    exporter.shutdown()


def test_b6_after_shutdown_export_fails_and_flush_false():
    """After shutdown, export() returns FAILURE; with a slow inner,
    force_flush returns False on timeout."""
    exporter = BufferedOtlpExporter(CollectingExporter(), _make_config())
    exporter.shutdown()
    assert exporter.export([object()]) == SpanExportResult.FAILURE

    slow = BufferedOtlpExporter(SlowExporter(delay=2.0), _make_config())
    slow.export([object()])
    assert slow.force_flush(timeout_millis=150) is False
    slow.shutdown()


# ---- reliability ----


def test_r1_concurrent_exports_no_loss_no_corruption(make_span):
    """8 threads exporting concurrently (within capacity): all arrive, no
    duplicates or corruption."""
    inner = CollectingExporter()
    exporter = BufferedOtlpExporter(inner, _make_config(queue_size=8192))

    def _produce(worker: int) -> None:
        for index in range(100):
            exporter.export([make_span(name=f"w{worker}-{index}")])

    threads = [threading.Thread(target=_produce, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert exporter.force_flush(timeout_millis=10000)
    names = [span.name for span in inner.exported]
    assert len(names) == 800
    assert len(set(names)) == 800  # no duplicates
    exporter.shutdown()


def test_r2_recovers_after_consecutive_failures(make_span):
    """Explicit recovery after sustained inner failures: failed batches get
    drop warnings, the thread stays alive, and post-recovery spans arrive."""
    inner = FlakyExporter()
    exporter = BufferedOtlpExporter(inner, _make_config(batch_size=2))
    for index in range(4):
        exporter.export([make_span(name=f"first-{index}")])

    assert _wait_until(lambda: inner.attempts >= 2 and inner.failures >= 2)
    inner.fail_enabled = False  # failure cleared
    for index in range(2):
        exporter.export([make_span(name=f"second-{index}")])
    assert _wait_until(
        lambda: {"second-0", "second-1"}.issubset({s.name for s in inner.exported}),
        timeout=5,
    )
    assert exporter.force_flush(timeout_millis=5000)
    exporter.shutdown()
    assert inner.attempts >= 3  # batches still delivered after recovery (thread alive)


def test_r3_failure_result_without_exception_is_logged(make_span):
    """Inner returning FAILURE without raising (the official exporter's
    network-failure shape): must emit a warning, never drop batches silently."""

    class _Captor(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    target = logging.getLogger("agentarts_instrumentation.exporter.buffer")
    captor = _Captor()
    target.addHandler(captor)
    try:
        # export_attempts=1: a single failure counts as a drop (retry/backoff
        # is covered by the dedicated R3b tests)
        exporter = BufferedOtlpExporter(
            FailingResultExporter(), _make_config(export_attempts=1)
        )
        for index in range(3):
            exporter.export([make_span(name=f"f-{index}")])
        assert exporter.force_flush(timeout_millis=5000)
        exporter.shutdown()
    finally:
        target.removeHandler(captor)

    assert any("batch export returned" in message for message in captor.messages), captor.messages
    assert any("FAILURE" in message for message in captor.messages)


def test_r3b_failure_retried_then_dropped(make_span):
    """Bounded retries: a failed batch retries with exponential backoff
    (attempt warnings) and only counts as dropped once exhausted -- a slow
    link's single RPC timeout no longer discards the whole batch (fix for a
    real-world DEADLINE_EXCEEDED)."""

    class _Captor(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    class _FailTwiceThenSuccess(CollectingExporter):
        """Fails the first two attempts (simulated transient network
        timeouts), succeeds on the third.

        Note the separate counter: CollectingExporter.export also bumps
        self.calls (the success path double-counts via super()), so that
        attribute cannot be reused for assertions.
        """

        def __init__(self) -> None:
            super().__init__()
            self.attempt_calls = 0

        def export(self, spans) -> SpanExportResult:
            self.attempt_calls += 1
            if self.attempt_calls <= 2:
                return SpanExportResult.FAILURE
            return super().export(spans)

    target = logging.getLogger("agentarts_instrumentation.exporter.buffer")
    captor = _Captor()
    target.addHandler(captor)
    inner = _FailTwiceThenSuccess()
    try:
        exporter = BufferedOtlpExporter(inner, _make_config())
        exporter.export([make_span(name="r3b-probe")])
        # Succeeds after 2 retries: force_flush completes within the backoff
        # (1s) plus the send
        assert exporter.force_flush(timeout_millis=10000)
        exporter.shutdown()
    finally:
        target.removeHandler(captor)

    assert inner.attempt_calls == 3
    assert [s.name for s in inner.exported] == ["r3b-probe"]  # span not lost after successful retry
    assert any("retrying after backoff" in m for m in captor.messages), captor.messages
    assert not any("drop" in m for m in captor.messages), captor.messages
