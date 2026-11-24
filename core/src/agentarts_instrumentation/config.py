# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exporter configuration models (design.md §4.1 / §4.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Projection(BaseModel):
    """Span attribute projection applied at export time.

    The projection operates on a clone seen by the wrapped exporter;
    the original span is never modified.

    Attributes:
        preset: Name of a registered semantic derivation applied at export
            time. ``none`` disables derivation and only applies the explicit
            rules below. The ``agentarts`` preset (openjiuwen data contract
            -> AgentArts gen_ai.* semantics) is registered by
            agentarts-instrumentation-openjiuwen; other framework packs register
            their own names.
        attributes: Common attributes injected into every exported span;
            never overwrites existing keys.
        drop: Attribute keys to drop. Exact names or ``prefix*`` form.
        truncate: Max length for string attribute values; 0 disables.
            Applies to str values only.
        rename: Key rename map; never overwrites an existing target key.
        service_name: Rewrites resource ``service.name`` on the exported
            clone. AgentArts requires ``AgentArts.<agent_id>.default``
            (see the platform doc "Report Trace Data"), which differs
            from the openjiuwen main stack; empty string disables the
            rewrite.
        skill_path_patterns: Path features for skill detection (consumed
            by the ``agentarts`` preset, design.md §4.3.5). Entries ending
            in ``.md`` match by file name, others by directory segment.
    """

    preset: str = "none"
    attributes: dict[str, str] = Field(default_factory=dict)
    drop: list[str] = Field(default_factory=list)
    truncate: int = Field(default=0, ge=0)
    rename: dict[str, str] = Field(default_factory=dict)
    service_name: str = ""
    skill_path_patterns: list[str] = Field(default_factory=lambda: ["SKILL.md", "skills"])


class ExporterConfig(BaseModel):
    """Construction config for BufferedOtlpExporter (design.md §4.1).

    Attributes:
        endpoint: OTLP endpoint. HTTP needs a full URL (including
            ``/v1/traces``); gRPC takes ``host:port`` or a URL form
            (default ``localhost:4317``).
        protocol: OTLP transport. ``http`` uses OTLP HTTP/protobuf
            (default, installed with the package); ``grpc`` uses OTLP
            gRPC (requires the ``grpc`` extra).
        headers: Auth and custom headers, passed straight to the
            official exporter (Basic/Bearer/API-key assembled by caller).
        projection: Attribute projection config; ``None`` disables it.
        queue_size: Bounded queue capacity (count); drops oldest when
            full and warns.
        queue_max_bytes: Bounded queue capacity (estimated bytes). The
            count limit alone cannot bound memory amplification from
            large-payload spans (LLM span input/output.messages
            measured ~300KB each; 4096 entries could accumulate GBs).
            Byte limit evicts oldest the same way — first limit hit
            wins.
        batch_size: Max spans per background send. Kept small so one
            request stays a bounded payload: measured (2026-11-24) that
            64-span batches carrying large LLM input/output payloads
            hit DEADLINE_EXCEEDED on a slow gateway far more often than
            32-span ones.
        flush_interval_ms: Background flush interval.
        otlp_timeout_ms: Per-RPC timeout of the inner official exporter
            (total deadline including its internal retries). Measured
            on slow cross-network links, a small timeout causes
            DEADLINE_EXCEEDED whole-batch failures — must comfortably
            exceed single-batch RTT + transfer time. 30s absorbs the
            subagent-burst slow-gateway windows observed (2026-11-24,
            where 15s lost whole 64-span batches).
        export_attempts: Total attempts per failed batch (including
            the first); retries with exponential backoff (1/2/4/8/16/30s
            capped) and only counts as dropped when exhausted. Default
            10 × 30s RPC + ~151s backoff ≈ 7.5 minutes of outage
            survival (measured gateway outages up to ~90s and
            slow-gateway burst streaks of several minutes, 2026-11-24;
            6 × 15s + 10s-cap ≈ 2min still dropped 2 × 64 spans then).
        export_timeout_ms: Drain timeout for shutdown/force_flush,
            aligned with the openjiuwen main stack semantics.
    """

    endpoint: str = Field(min_length=1)
    protocol: Literal["grpc", "http"] = "http"
    headers: dict[str, str] = Field(default_factory=dict)
    projection: Projection | None = None
    queue_size: int = Field(default=4096, ge=1)
    queue_max_bytes: int = Field(default=134217728, ge=1024)
    batch_size: int = Field(default=32, ge=1)
    flush_interval_ms: int = Field(default=500, ge=1)
    otlp_timeout_ms: int = Field(default=30000, ge=100)
    export_attempts: int = Field(default=10, ge=1)
    export_timeout_ms: int = Field(default=5000, ge=100)

    @field_validator("headers")
    @classmethod
    def _reject_control_characters(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject control characters (incl. CRLF) in header keys/values.

        gRPC metadata rejects invalid keys at a lower level anyway, but
        HTTP channel behavior with CR/LF in values depends on the
        underlying library; rejecting here minimizes the attack surface.
        """
        for key, header_value in value.items():
            for text, label in ((key, "key"), (header_value, "value")):
                if any(ord(char) < 32 for char in str(text)):
                    raise ValueError(
                        f"headers {label} contains control characters "
                        f"(CRLF injection risk): {key!r}"
                    )
        return value
