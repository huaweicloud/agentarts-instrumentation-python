# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for BufferedOtlpExporter and the factory (design.md §4.5/§4.6)."""

from __future__ import annotations

import time

from agentarts_instrumentation.config import ExporterConfig, Projection
from agentarts_instrumentation.exporter.buffer import (
    BufferedOtlpExporter,
    _estimate_span_bytes,
)
from agentarts_instrumentation.exporter.transform import TransformingSpanExporter
from agentarts_instrumentation.factory import build_exporter
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class FakeExporter(SpanExporter):
    def __init__(self):
        self.exported: list = []
        self.batches: list[list] = []
        self.flush_calls: list[int] = []
        self.shutdown_count = 0
        self.fail_on_export = False

    def export(self, spans):
        if self.fail_on_export:
            # Simulate "send failure means data never arrives": raise first, don't record.
            self.batches.append(list(spans))
            raise RuntimeError("boom")
        self.batches.append(list(spans))
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000):
        self.flush_calls.append(timeout_millis)
        return True

    def shutdown(self):
        self.shutdown_count += 1


def _config(**overrides) -> ExporterConfig:
    defaults = {
        "endpoint": "http://localhost:4318/v1/traces",
        "queue_size": 100,
        "batch_size": 64,
        "flush_interval_ms": 30000,
        "export_timeout_ms": 5000,
    }
    defaults.update(overrides)
    return ExporterConfig(**defaults)


def test_export_reaches_inner_via_force_flush(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config())
    spans = [make_span(f"s{i}") for i in range(3)]
    start = time.monotonic()
    assert exporter.export(spans) is SpanExportResult.SUCCESS
    assert time.monotonic() - start < 0.5  # returns right after enqueue, non-blocking
    assert exporter.force_flush(timeout_millis=5000) is True
    assert len(inner.exported) == 3
    assert inner.flush_calls  # inner force_flush is delegated
    exporter.shutdown()


def test_force_flush_drains_queue(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config(flush_interval_ms=30000))
    exporter.export([make_span("a"), make_span("b")])
    assert inner.exported == []  # background thread hasn't sent yet at long interval
    assert exporter.force_flush(timeout_millis=5000) is True
    assert len(inner.exported) == 2
    exporter.shutdown()


def test_queue_full_drops_oldest(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config(queue_size=2, batch_size=64))
    a, b, c = make_span("a"), make_span("b"), make_span("c")
    exporter.export([a, b, c])
    assert exporter.force_flush(timeout_millis=5000) is True
    assert [s.name for s in inner.exported] == ["b", "c"]  # oldest "a" dropped
    assert exporter._dropped == 1
    exporter.shutdown()


def test_queue_byte_cap_drops_oldest(make_span):
    """Byte cap triggers before the count cap: large-payload spans evict the
    oldest; accounting resets to zero on dequeue.

    The count limit alone cannot prevent memory blowup -- a single LLM span
    can reach ~300KB, so 4096 entries could be GB-level in theory. When
    queue_max_bytes is reached the oldest is dropped as well (whichever cap
    hits first wins)."""
    payload = "x" * 5000
    per_span = _estimate_span_bytes(make_span("s0", attributes={"payload": payload}))
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(
        inner, _config(queue_size=100, queue_max_bytes=2 * per_span)
    )
    exporter.export(
        [make_span(f"s{i}", attributes={"payload": payload}) for i in range(4)]
    )
    # Capacity 2*per: s0/s1 fill it -> s2/s3 each evict the two oldest on enqueue
    assert len(exporter._queue) == 2
    assert [s.name for s in (span for span, _ in exporter._queue)] == ["s2", "s3"]
    assert exporter._dropped == 2
    assert exporter._queued_bytes == 2 * per_span
    assert exporter.force_flush(timeout_millis=5000) is True
    assert [s.name for s in inner.exported] == ["s2", "s3"]
    assert exporter._queued_bytes == 0  # accounting resets on dequeue
    exporter.shutdown()


def test_queue_oversized_single_span_dropped(make_span):
    """Single span exceeding the whole byte budget: enqueueing would always
    exceed, so drop it directly and count it."""
    payload = "x" * 5000
    per_span = _estimate_span_bytes(make_span("big", attributes={"payload": payload}))
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(
        inner, _config(queue_size=100, queue_max_bytes=per_span - 1)
    )
    exporter.export([make_span("big", attributes={"payload": payload})])
    assert len(exporter._queue) == 0
    assert exporter._queued_bytes == 0
    assert exporter._dropped == 1
    assert exporter.force_flush(timeout_millis=5000) is True
    assert inner.exported == []
    exporter.shutdown()


def test_shutdown_drains_pending(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config(flush_interval_ms=30000))
    exporter.export([make_span("a"), make_span("b")])
    exporter.shutdown()
    assert len(inner.exported) == 2  # drained before shutdown
    assert inner.shutdown_count == 1


def test_export_after_shutdown_fails(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config())
    exporter.shutdown()
    assert exporter.export([make_span()]) is SpanExportResult.FAILURE
    assert inner.exported == []


def test_shutdown_idempotent(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config())
    exporter.shutdown()
    exporter.shutdown()
    assert inner.shutdown_count == 1


def test_inner_export_exception_does_not_kill_thread(make_span):
    # export_attempts=1: an exception counts as a drop (the retry/backoff path
    # is covered by dedicated R3b tests); this case focuses on "thread stays
    # alive and later exports work" semantics and stays fast.
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config(export_attempts=1))
    inner.fail_on_export = True
    exporter.export([make_span("bad")])
    assert exporter.force_flush(timeout_millis=5000) is True
    inner.fail_on_export = False
    exporter.export([make_span("good")])
    assert exporter.force_flush(timeout_millis=5000) is True
    assert [s.name for s in inner.exported] == ["good"]  # thread alive, later exports work
    exporter.shutdown()


def test_empty_export_short_circuits(make_span):
    inner = FakeExporter()
    exporter = BufferedOtlpExporter(inner, _config())
    assert exporter.export([]) is SpanExportResult.SUCCESS
    exporter.shutdown()


# ---- factory ----


def test_build_exporter_without_projection():
    exporter = build_exporter(_config())
    assert isinstance(exporter, BufferedOtlpExporter)
    exporter.shutdown()


def test_build_exporter_with_identity_projection_no_wrap():
    exporter = build_exporter(_config(projection=Projection()))
    assert isinstance(exporter, BufferedOtlpExporter)
    exporter.shutdown()


def test_build_exporter_with_projection_wraps():
    exporter = build_exporter(_config(projection=Projection(preset="agentarts")))
    assert isinstance(exporter, TransformingSpanExporter)
    assert isinstance(exporter.exporter, BufferedOtlpExporter)
    exporter.shutdown()


def test_projection_integrated_with_buffer(make_span):
    """Full projection -> enqueue -> force_flush pipeline: the projected copy
    reaches the inner exporter (no real network)."""
    from agentarts_instrumentation.projection import project_span

    inner = FakeExporter()
    buffered = BufferedOtlpExporter(inner, _config())
    projection = Projection(preset="agentarts", drop=["secret*"])
    exporter = TransformingSpanExporter(
        buffered,
        transform=lambda span: project_span(span, projection),
    )
    span = make_span(
        "chat m",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
            "secret.key": "v",
        },
    )
    exporter.export([span])
    assert exporter.force_flush(timeout_millis=5000) is True
    assert len(inner.exported) == 1
    attrs = inner.exported[0].attributes
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.usage.total_tokens"] == 15
    assert "secret.key" not in attrs
    assert span.attributes["secret.key"] == "v"  # original span untouched
    exporter.shutdown()
