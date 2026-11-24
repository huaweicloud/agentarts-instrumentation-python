# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""jiuwenswarm e2e-specific fixtures (environment bootstrap in tests/conftest.py).

Requires both the agent-core and jiuwenswarm local checkouts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[3]
_AGENT_CORE_DIR = Path(os.environ.get("AGENT_CORE_DIR", _WORKSPACE / "agent-core"))
_JIUWENSWARM_DIR = Path(os.environ.get("JIUWENSWARM_DIR", _WORKSPACE / "jiuwenswarm"))
_AGENT_GATEWAY_DIR = Path(os.environ.get("AGENT_GATEWAY_DIR", _WORKSPACE / "agent-gateway"))

if not (
    _AGENT_CORE_DIR.is_dir()
    and _JIUWENSWARM_DIR.is_dir()
    and _AGENT_GATEWAY_DIR.is_dir()
    and (_WORKSPACE / "agent-protocol" / "e2a" / "e2a").is_dir()
):
    pytest.skip(
        "agent-core / jiuwenswarm / agent-gateway / agent-protocol checkout not found",
        allow_module_level=True,
    )

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
def swarm_trace_root(tmp_path: Path) -> Path:
    return tmp_path / "trace"


@pytest.fixture
def swarm_trajectory(swarm_trace_root: Path):
    """Start the real jiuwenswarm trajectory data plane (SQLite sink);
    shut down and drain after the test."""
    from jiuwenswarm.observability.config import TrajectoryStoreSettings
    from jiuwenswarm.observability.runtime import (
        shutdown_trajectory_runtime,
        start_trajectory_runtime,
    )

    settings = TrajectoryStoreSettings(
        enabled=True,
        database_path=swarm_trace_root / "sessions",
        retention_days=0,
        queue_size=64,
        batch_size=8,
        flush_interval_ms=50,
    )
    start_trajectory_runtime(settings)
    yield settings
    shutdown_trajectory_runtime()


@pytest.fixture
def swarm_sdk_exporter() -> TransformingSpanExporter:
    """SDK exporter: agentarts preset + drop secret*, to verify isolation
    from the data plane."""
    inner = CollectingExporter()
    buffered = BufferedOtlpExporter(
        inner,
        ExporterConfig(endpoint="http://localhost:4318/v1/traces"),
    )
    projection = Projection(preset="agentarts", drop=["secret*"])
    exporter = TransformingSpanExporter(
        buffered,
        transform=lambda span: project_span(span, projection),
    )
    exporter.buffered = buffered  # type: ignore[attr-defined]
    exporter.collecting = inner  # type: ignore[attr-defined]
    return exporter
