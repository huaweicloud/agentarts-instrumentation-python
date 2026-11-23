# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Official OTLP exporter construction (protocol selection, timeout,
TLS/insecure inference)."""

from __future__ import annotations

from opentelemetry.sdk.trace.export import SpanExporter

from ..config import ExporterConfig


def build_otlp_exporter(config: ExporterConfig) -> SpanExporter:
    """Build the official OTLP exporter (timeout in seconds).

    The RPC timeout is ``otlp_timeout_ms`` (total deadline per export,
    including official internal retries); ``export_timeout_ms`` only
    governs BufferedOtlpExporter's shutdown drain.
    """
    timeout = config.otlp_timeout_ms / 1000.0
    if config.protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )

        return HttpExporter(
            endpoint=config.endpoint,
            headers=dict(config.headers),
            timeout=timeout,
        )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )

    # insecure is only valid for plaintext (http/local collector)
    # endpoints; https endpoints must use a secure (TLS) channel —
    # forcing insecure=True overrides the official exporter's scheme
    # inference and sends plaintext gRPC to the TLS gateway (Huawei
    # Cloud APM access returns 504).
    insecure = not config.endpoint.startswith("https://")
    return GrpcExporter(
        endpoint=config.endpoint,
        insecure=insecure,
        headers=dict(config.headers),
        timeout=timeout,
    )


__all__ = ["build_otlp_exporter"]
