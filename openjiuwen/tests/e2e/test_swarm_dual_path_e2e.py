# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""jiuwenswarm dual-path e2e (design.md §8: file as primary exporter + SDK
append coexisting).

Replays the production onboarding sequence:
1. the jiuwenswarm trajectory data plane (real SQLite sink) starts first;
2. acquire/init builds the shared provider (file primary exporter +
   SpanRecordProcessor);
3. the SDK appends via span_exporter_override (§6.2 zero-change path);
4. one span is dispatched on both paths -- the SDK receives the
   projected/trimmed copy, the SQLite data plane receives the full canonical
   record (invariant 5: data-plane isolation).
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

from opentelemetry.sdk.trace.export import SpanExportResult

_FLUSH_TIMEOUT_MS = 8000
_SESSION_ID = "e2e-session-0001"


def _decode_raw_payload(raw: bytes) -> bytes:
    """The SQLite side stores raw_json zlib-compressed (store.py:385); the
    WAL JSONL is plain text."""
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def _otlp_span_attributes(raw_json: bytes) -> dict:
    """Decode attribute key/values from single-span OTLP JSON
    (ExportTraceServiceRequest shape)."""
    payload = json.loads(_decode_raw_payload(raw_json))
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    decoded: dict = {}
    for attr in span.get("attributes", []):
        value = attr["value"]
        decoded[attr["key"]] = next(iter(value.values()))
    return decoded


def test_swarm_dual_path_e2e(
    swarm_trajectory,
    swarm_sdk_exporter,
    tmp_path: Path,
):
    from jiuwenswarm.observability.config import session_database_path
    from openjiuwen.extensions.observability import setup as oj_setup
    from openjiuwen.extensions.observability.config import ObservabilityConfig
    from openjiuwen.extensions.observability.demand import get_span_record_processor

    # ---- primary exporter: file (jiuwenswarm production default), plus SpanRecordProcessor ----
    traces_dir = tmp_path / "traces"
    oj_cfg = ObservabilityConfig(
        enabled=True,
        service_name="huawei-otel-exporter-e2e",
        exporter="file",
        traces_dir=str(traces_dir),
    )
    oj_setup.init_observability(
        oj_cfg,
        additional_span_processors=(get_span_record_processor(),),
    )
    assert oj_setup.is_initialized() is True

    # ---- SDK append (provider already exists -> SimpleSpanProcessor, dual path) ----
    oj_setup.init_observability(oj_cfg, span_exporter_override=swarm_sdk_exporter)

    # ---- produce a span: session attribution, usage, and a sensitive
    # attribute the SDK must drop ----
    tracer = oj_setup.get_tracer("huawei-otel-exporter-e2e")
    with tracer.start_as_current_span(
        "chat e2e-model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": _SESSION_ID,
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.usage.output_tokens": 3,
            "secret.key": "must-not-leak",
        },
    ):
        pass

    # ---- close-out: SDK drains -> provider shuts down -> sink drains to DB
    # (production order: provider closes first) ----
    assert swarm_sdk_exporter.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS) is True
    oj_setup.shutdown_observability()
    from jiuwenswarm.observability.runtime import shutdown_trajectory_runtime

    assert shutdown_trajectory_runtime() is True

    # ---- SDK-side assertions: projected + trimmed copy ----
    sdk_spans = swarm_sdk_exporter.collecting.exported
    assert len(sdk_spans) == 1
    sdk_attrs = sdk_spans[0].attributes
    assert sdk_attrs["gen_ai.span.kind"] == "LLM"
    assert sdk_attrs["gen_ai.usage.total_tokens"] == 10
    assert "secret.key" not in sdk_attrs
    assert swarm_sdk_exporter.collecting.shutdown_count == 1

    # ---- SQLite-side assertions: full canonical, unpolluted by projection ----
    database = session_database_path(swarm_trajectory.database_path, _SESSION_ID)
    assert database.is_file(), f"session database not created: {database}"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT raw_json FROM otlp_span_records WHERE session_id = ?",
            (_SESSION_ID,),
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, f"expected 1 span record, got {len(rows)}"
    store_attrs = _otlp_span_attributes(rows[0][0])
    assert store_attrs["secret.key"] == "must-not-leak"
    assert store_attrs["gen_ai.usage.input_tokens"] == "7"
    assert store_attrs["gen_ai.usage.output_tokens"] == "3"
    assert "gen_ai.span.kind" not in store_attrs
    assert "gen_ai.usage.total_tokens" not in store_attrs

    # ---- the file primary exporter also works (dual path): the traces
    # directory already has JSONL ----
    trace_files = list(traces_dir.glob("traces-*.jsonl"))
    assert trace_files, "file exporter did not produce trace WAL"
    wal_lines = trace_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert wal_lines
    wal_attrs = _otlp_span_attributes(wal_lines[0].encode("utf-8"))
    assert wal_attrs["secret.key"] == "must-not-leak"


def test_swarm_exporter_shutdown_drains_queue(
    make_span,
    swarm_sdk_exporter,
):
    """When provider shutdown propagates to the SDK, the queue is drained
    (no pending spans lost)."""
    from openjiuwen.extensions.observability import setup as oj_setup
    from openjiuwen.extensions.observability.config import ObservabilityConfig

    oj_cfg = ObservabilityConfig(
        enabled=True,
        service_name="huawei-otel-exporter-e2e",
        exporter="console",
    )
    oj_setup.init_observability(oj_cfg, span_exporter_override=swarm_sdk_exporter)
    tracer = oj_setup.get_tracer("huawei-otel-exporter-e2e")
    with tracer.start_as_current_span("chat e2e-model", attributes={"gen_ai.operation.name": "chat"}):
        pass

    oj_setup.shutdown_observability()
    exported = swarm_sdk_exporter.collecting.exported
    assert len(exported) == 1
    assert exported[0].attributes["gen_ai.span.kind"] == "LLM"
    # already shut down: a later (non-empty) export returns FAILURE and
    # never enqueues
    assert swarm_sdk_exporter.buffered.export([make_span()]) is SpanExportResult.FAILURE
