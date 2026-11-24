# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agentarts-instrumentation-core: OTLP exporter kernel for Huawei Cloud
AgentArts (agent-ops).

Framework-agnostic building blocks: the bounded-buffer OTLP exporter, the
copy-on-export span projection pipeline, the exporter factory, and the
runtime-injection toolkit. Framework packs (e.g.
agentarts-instrumentation-openjiuwen) register their semantic derivations here
and drive injection into host runtimes. See docs/design.md.
"""

from __future__ import annotations

__path__ = __import__("pkgutil").extend_path(__path__, __name__)

from ._version import __version__
from .config import ExporterConfig, Projection
from .exporter.buffer import BufferedOtlpExporter
from .exporter.transform import TransformingSpanExporter
from .factory import build_exporter
from .projection import DerivationOutcome, project_span, register_derivation

__all__ = [
    "BufferedOtlpExporter",
    "DerivationOutcome",
    "ExporterConfig",
    "Projection",
    "TransformingSpanExporter",
    "build_exporter",
    "project_span",
    "register_derivation",
    "__version__",
]
