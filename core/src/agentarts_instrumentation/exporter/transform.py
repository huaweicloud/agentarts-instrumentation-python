# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Export-time span transformation: clone + transform wrapper exporter.

Semantically aligned with agent-core
``extensions/observability/exporters/transforming.py`` (copy-on-export,
fall back to exporting the original span on transform failure) but with
zero dependency on openjiuwen. The projection only touches the clone
seen by the wrapped exporter; other consumers on the processor chain
(SpanRecordProcessor / SQLite trajectory store, etc.) are unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter

logger = logging.getLogger(__name__)

#: Read-only span transform: returns a new span, never mutates the input.
SpanTransform = Callable[[ReadableSpan], ReadableSpan]


def clone_readable_span(
    span: ReadableSpan,
    attributes: Mapping[str, Any],
    events: Sequence[Any] | None = None,
    resource: Any = None,
    name: str | None = None,
    links: Sequence[Any] | None = None,
    status: Any = None,
) -> ReadableSpan:
    """Return a full copy of the span, replacing only ``attributes``
    (events/links/resource/name/status optional).

    Identity fields are preserved as-is: trace/span id, parent context,
    kind, timestamps and instrumentation scope all reference the
    originals; status / resource / links / name also reference the
    originals unless explicitly replaced. ``name`` powers the display
    name rewrite (preset §4.3.7a); ``status`` powers AGENT root error
    propagation (preset §4.3.9); ``links`` powers the preset's link
    clearing (pass an empty sequence).
    """

    return ReadableSpan(
        name=span.name if name is None else name,
        context=span.get_span_context(),
        parent=span.parent,
        resource=span.resource if resource is None else resource,
        attributes=dict(attributes),
        events=span.events if events is None else list(events),
        links=span.links if links is None else list(links),
        kind=span.kind,
        status=span.status if status is None else status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class TransformingSpanExporter(SpanExporter):
    """Wraps an exporter behind a per-span read-only transform.

    Every ended span is cloned first and then handed to ``transform``;
    the wrapped exporter only sees projected clones. Two special return
    semantics:

    - ``transform`` returns ``None``: **intentional filter** — the span
      is not exported (used by the preset to drop trajectory-protocol
      spans);
    - ``transform`` raises: **failure fallback** — log a warning and
      export the original span; a projection bug must never lose
      telemetry.
    """

    def __init__(self, exporter: SpanExporter, transform: SpanTransform) -> None:
        self._exporter = exporter
        self._transform = transform

    @property
    def exporter(self) -> SpanExporter:
        """The wrapped exporter."""
        return self._exporter

    def export(self, spans: Sequence[ReadableSpan]) -> Any:
        """Transform each span, then export; None = intentional skip,
        failure falls back to the original."""
        projected: list[ReadableSpan] = []
        for span in spans or ():
            try:
                transformed = self._transform(span)
            except Exception as exc:
                logger.warning(
                    "span transform %s failed for span %s - exporting original: %s",
                    getattr(self._transform, "__name__", type(self._transform).__name__),
                    getattr(span, "name", "<unknown>"),
                    exc,
                )
                projected.append(span)
                continue
            if transformed is None:
                continue
            projected.append(transformed)
        return self._exporter.export(projected)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Delegates to the wrapped exporter."""
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        """Delegates to the wrapped exporter."""
        self._exporter.shutdown()


__all__ = [
    "SpanTransform",
    "TransformingSpanExporter",
    "clone_readable_span",
]
