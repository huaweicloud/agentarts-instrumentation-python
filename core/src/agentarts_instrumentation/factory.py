# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exporter factory (design.md §4.6).

Assembly order: official OTLP exporter (inner; protocol/retry/
compression handled upstream) → BufferedOtlpExporter (batch buffering)
→ optional TransformingSpanExporter (attribute projection, applied on
the calling thread before enqueue).
"""

from __future__ import annotations

from opentelemetry.sdk.trace.export import SpanExporter

from .config import ExporterConfig, Projection
from .exporter.buffer import BufferedOtlpExporter
from .exporter.otlp import build_otlp_exporter
from .exporter.transform import TransformingSpanExporter
from .projection import get_derivation, project_span


def build_exporter(config: ExporterConfig) -> SpanExporter:
    """Assemble the SDK exporter from config.

    Inject into the shared provider via the host runtime's exporter
    override hook (e.g. openjiuwen agent-core
    ``init_observability(..., span_exporter_override=...)``); also
    usable standalone as a plain SpanExporter.
    """
    projection = config.projection
    if projection is not None and _projection_active(projection):
        # Validate the preset name early (clear error instead of a
        # failure deep in the export path).
        get_derivation(projection.preset)
    inner = build_otlp_exporter(config)
    buffered = BufferedOtlpExporter(inner, config)
    if projection is not None and _projection_active(projection):
        return TransformingSpanExporter(
            buffered,
            transform=lambda span: project_span(span, projection),
        )
    return buffered


def _projection_active(projection: Projection) -> bool:
    return (
        projection.preset not in (None, "", "none")
        or bool(projection.attributes)
        or bool(projection.drop)
        or bool(projection.rename)
        or projection.truncate > 0
        or bool(projection.service_name)
    )


__all__ = ["build_exporter"]
