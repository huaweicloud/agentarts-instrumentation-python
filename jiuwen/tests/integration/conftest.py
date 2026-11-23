# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-core integration-test fixtures (environment bootstrap in tests/conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_AGENT_CORE_DIR = Path(
    os.environ.get("AGENT_CORE_DIR", Path(__file__).resolve().parents[3] / "agent-core")
)

if not _AGENT_CORE_DIR.is_dir():
    pytest.skip(f"agent-core checkout not found: {_AGENT_CORE_DIR}", allow_module_level=True)

from agentarts_instrumentation.config import ExporterConfig, Projection
from agentarts_instrumentation.exporter.buffer import BufferedOtlpExporter
from agentarts_instrumentation.exporter.transform import TransformingSpanExporter
from agentarts_instrumentation.projection import project_span
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class CollectingExporter(SpanExporter):
    """Fake inner exporter that records exported spans (no network)."""

    def __init__(self) -> None:
        self.exported: list = []
        self.shutdown_count = 0

    def export(self, spans) -> SpanExportResult:
        self.exported.extend(list(spans))
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_count += 1


@pytest.fixture
def collecting_exporter() -> CollectingExporter:
    return CollectingExporter()


@pytest.fixture
def sdk_exporter(collecting_exporter: CollectingExporter) -> TransformingSpanExporter:
    """Build the SDK exporter the way the factory does (agentarts projection
    + buffering), with a collecting fake as the inner exporter."""
    buffered = BufferedOtlpExporter(
        collecting_exporter,
        ExporterConfig(endpoint="http://localhost:4318/v1/traces"),
    )
    projection = Projection(preset="agentarts")
    exporter = TransformingSpanExporter(
        buffered,
        transform=lambda span: project_span(span, projection),
    )
    exporter.buffered = buffered  # type: ignore[attr-defined]
    exporter.collecting = collecting_exporter  # type: ignore[attr-defined]
    return exporter


@pytest.fixture
def observability():
    """Provide the openjiuwen observability facade; forcibly clean up the
    shared provider after the test."""
    from openjiuwen.extensions.observability import setup as oj_setup

    yield oj_setup
    oj_setup.shutdown_observability()


@pytest.fixture
def oj_config_factory():
    from openjiuwen.extensions.observability.config import ObservabilityConfig

    def _make(exporter: str = "console") -> ObservabilityConfig:
        return ObservabilityConfig(
            enabled=True,
            service_name="huawei-otel-exporter-integration",
            exporter=exporter,
        )

    return _make
