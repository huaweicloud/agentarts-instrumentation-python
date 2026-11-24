# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Test environment bootstrap + shared fixtures.

At conftest load time, adds local agent-core / jiuwenswarm checkouts to
sys.path (overridable via AGENT_CORE_DIR / JIUWENSWARM_DIR), and patches the
openjiuwen LogManager with a stdlib fallback (aligned with agent-core CLI
test practice; test environments lack the
extensions.common.configs.log_config entry-point).

If a checkout is missing, the integration / e2e sub-directory conftests
decide whether to skip.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# Importing the openjiuwen pack registers the "agentarts" derivation for
# Projection(preset="agentarts") used across these tests.
import agentarts_instrumentation.openjiuwen  # noqa: F401, E402
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

_WORKSPACE = Path(__file__).resolve().parents[2]


def _resolve_checkout(name: str, env_name: str) -> Path:
    """Locate a local checkout via env var or the default workspace layout and add it to sys.path."""
    default = _WORKSPACE / name
    path = Path(os.environ.get(env_name, default))
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
    return path


AGENT_CORE_DIR = _resolve_checkout("agent-core", "AGENT_CORE_DIR")
JIUWENSWARM_DIR = _resolve_checkout("jiuwenswarm", "JIUWENSWARM_DIR")
AGENT_GATEWAY_DIR = _resolve_checkout("agent-gateway", "AGENT_GATEWAY_DIR")
# The e2a package lives in the e2a sub-directory of the agent-protocol repo (src-style layout).
if AGENT_GATEWAY_DIR.is_dir():
    _resolve_checkout(str(AGENT_GATEWAY_DIR.parent / "agent-protocol" / "e2a"), "AGENT_PROTOCOL_DIR")


def _bootstrap_sdk_logging() -> None:
    """Fall back to a stdlib logger when LogManager is not configured (agent-core CLI test practice)."""
    if not AGENT_CORE_DIR.is_dir():
        return
    try:
        from openjiuwen.core.common.logging import manager as log_mgr

        log_manager = log_mgr.LogManager
        original_get_logger = log_manager.get_logger.__func__

        class _StdlibFallback:
            """Minimal LoggerProtocol implementation backed by stdlib logging."""

            def __init__(self, log_type: str, config: Any = None) -> None:
                self._logger = logging.getLogger(f"openjiuwen.{log_type}")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._logger, name)

            def logger(self) -> logging.Logger:
                return self._logger

        @classmethod  # type: ignore[misc]
        def _safe_get_logger(cls: type, log_type: str = "default") -> Any:
            try:
                return original_get_logger(cls, log_type)
            except RuntimeError:
                if log_type not in cls._loggers:
                    cls._loggers[log_type] = _StdlibFallback(log_type)
                return cls._loggers[log_type]

        log_manager.get_logger = _safe_get_logger
        log_manager._initialized = True
    except Exception:  # noqa: BLE001
        pass


_bootstrap_sdk_logging()

_TRACE_ID = 0x0102030405060708090A0B0C0D0E0F10
_SPAN_ID = 0x1122334455667788


@pytest.fixture
def make_span():
    """Build a ReadableSpan for projection/export tests.

    Optional ``span_id`` / ``parent_span_id`` support parent-child
    reference scenarios (task_iteration container detection and
    cross-span aggregation tree-shape tests).
    """

    def _make(
        name: str = "test-span",
        attributes: dict | None = None,
        events: list | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        span_id: int = _SPAN_ID,
        parent_span_id: int | None = None,
        trace_id: int = _TRACE_ID,
        status: Any = None,
    ) -> ReadableSpan:
        parent = None
        if parent_span_id is not None:
            parent = SpanContext(
                trace_id=trace_id,
                span_id=parent_span_id,
                is_remote=False,
                trace_flags=TraceFlags.SAMPLED,
            )
        return ReadableSpan(
            name=name,
            context=SpanContext(
                trace_id=trace_id,
                span_id=span_id,
                is_remote=False,
                trace_flags=TraceFlags.SAMPLED,
            ),
            parent=parent,
            resource=None,
            attributes=dict(attributes or {}),
            events=list(events or []),
            links=[],
            kind=kind,
            status=status,
            start_time=1,
            end_time=2,
            instrumentation_scope=None,
        )

    return _make
