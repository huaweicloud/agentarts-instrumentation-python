# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agentarts-instrumentation-openjiuwen: OpenJiuwen semantic pack for
Huawei Cloud AgentArts (agent-ops).

Registers the ``agentarts`` projection derivation (openjiuwen attribute
contract → AgentArts gen_ai.* platform semantics) and ships the runtime
injection for openjiuwen hosts (init_observability wrapper; see
inject.py — imported by the ``agentarts-instrumentation-inject`` .pth, never at package
import). Zero import dependency on openjiuwen: the derivation reads the
host's attribute contract only. See docs/design.md.
"""

from __future__ import annotations

__path__ = __import__("pkgutil").extend_path(__path__, __name__)

from agentarts_instrumentation import (
    BufferedOtlpExporter,
    ExporterConfig,
    Projection,
    TransformingSpanExporter,
    build_exporter,
)
from agentarts_instrumentation.projection import register_derivation

from . import preset
from ._version import __version__
from .preset import derive_agentarts

# Register the framework derivation for Projection(preset="agentarts").
# Importing agentarts_instrumentation.openjiuwen (directly or via the .pth
# injection) is what activates the preset.
register_derivation("agentarts", derive_agentarts)

__all__ = [
    "BufferedOtlpExporter",
    "ExporterConfig",
    "Projection",
    "TransformingSpanExporter",
    "build_exporter",
    "derive_agentarts",
    "preset",
    "__version__",
]
