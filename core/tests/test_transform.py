# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for transform.py: identity-preserving clone + fallback on transform failure."""

from __future__ import annotations

from agentarts_instrumentation.exporter.transform import (
    TransformingSpanExporter,
    clone_readable_span,
)
from opentelemetry.sdk.trace.export import SpanExporter


class FakeExporter(SpanExporter):
    def __init__(self):
        self.batches: list[list] = []
        self.flush_calls: list[int] = []
        self.shutdown_count = 0
        self.fail_on_export = False

    def export(self, spans):
        self.batches.append(list(spans))
        if self.fail_on_export:
            raise RuntimeError("boom")
        return True

    def force_flush(self, timeout_millis: int = 30000):
        self.flush_calls.append(timeout_millis)
        return True

    def shutdown(self):
        self.shutdown_count += 1


def test_clone_preserves_identity(make_span):
    span = make_span("orig", attributes={"a": 1})
    cloned = clone_readable_span(span, {"b": 2})
    assert cloned is not span
    assert cloned.name == "orig"
    assert cloned.get_span_context().trace_id == span.get_span_context().trace_id
    assert cloned.get_span_context().span_id == span.get_span_context().span_id
    assert cloned.parent == span.parent
    assert cloned.resource == span.resource
    assert cloned.kind == span.kind
    assert cloned.status == span.status
    assert cloned.start_time == span.start_time
    assert cloned.end_time == span.end_time
    assert cloned.attributes == {"b": 2}
    assert span.attributes == {"a": 1}  # original span untouched


def test_clone_keeps_events_when_not_replaced(make_span):
    from opentelemetry.sdk.trace import Event

    events = [Event("e1", {"k": 1}, 100)]
    span = make_span("orig", events=events)
    cloned = clone_readable_span(span, span.attributes)
    assert list(cloned.events) == events


def test_clone_replaces_events_when_given(make_span):
    from opentelemetry.sdk.trace import Event

    old = [Event("old", None, 1)]
    new = [Event("new", None, 2)]
    span = make_span("orig", events=old)
    cloned = clone_readable_span(span, span.attributes, new)
    assert list(cloned.events) == new
    assert list(span.events) == old


def test_transform_failure_falls_back_to_original(make_span):
    inner = FakeExporter()

    def bad_transform(span):
        raise ValueError("bad projection")

    exporter = TransformingSpanExporter(inner, bad_transform)
    span = make_span("keep-me")
    exporter.export([span])
    assert inner.batches[0][0] is span  # falls back to exporting the original


def test_transform_none_filters_span_intentionally(make_span):
    """transform returning None = intentional filtering: that span doesn't
    reach the inner exporter; the rest export normally."""
    inner = FakeExporter()

    def drop_named(span):
        if span.name == "context.window.commit":
            return None
        return span

    exporter = TransformingSpanExporter(inner, drop_named)
    kept = make_span("keep-me")
    exporter.export([make_span("context.window.commit"), kept])
    assert len(inner.batches[0]) == 1
    assert inner.batches[0][0] is kept


def test_transform_applied_on_copy(make_span):
    inner = FakeExporter()

    def add_attr(span):
        return clone_readable_span(span, {**span.attributes, "added": True})

    exporter = TransformingSpanExporter(inner, add_attr)
    span = make_span("orig", attributes={"a": 1})
    exporter.export([span])
    exported = inner.batches[0][0]
    assert exported is not span
    assert exported.attributes["added"] is True
    assert "added" not in span.attributes


def test_transforming_delegates_flush_and_shutdown(make_span):
    inner = FakeExporter()
    exporter = TransformingSpanExporter(inner, lambda span: span)
    exporter.export([make_span()])
    assert exporter.force_flush(timeout_millis=123) is True
    assert inner.flush_calls == [123]
    exporter.shutdown()
    assert inner.shutdown_count == 1


def test_wrapped_exporter_property(make_span):
    inner = FakeExporter()
    exporter = TransformingSpanExporter(inner, lambda span: span)
    assert exporter.exporter is inner
    exporter.shutdown()
