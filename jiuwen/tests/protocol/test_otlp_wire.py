# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OTLP wire verification: real official exporter -> local mock AgentArts
receiver.

Proves that "just swap in a real trace_endpoint + trace_token and reporting
works": the full official OTLP HTTP/protobuf serialization and auth header
are exercised; the receiver decodes the ExportTraceServiceRequest and
checks the span shape AgentArts requires (service.name convention,
required common attributes, span.kind, total_tokens).
"""

from __future__ import annotations

import http.server
import threading

# This test drives the full agentarts-preset pipeline; importing the
# jiuwen pack registers the derivation (belt-and-braces next to the
# jiuwen/tests conftest, so the file also works standalone).
import agentarts_instrumentation.jiuwen  # noqa: F401
import pytest
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


@pytest.fixture
def otlp_receiver():
    """Local mock AgentArts OTLP/HTTP receiver recording request headers and
    the protobuf body."""
    received: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append(
                {
                    "path": self.path,
                    "authentication": self.headers.get("Authentication"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, received
    server.shutdown()
    server.server_close()


def _build_agentarts_exporter(endpoint: str):
    """Assemble the SDK exporter in AgentArts onboarding shape (design.md §6.4)."""
    return build_exporter(
        ExporterConfig(
            endpoint=endpoint,
            protocol="http",
            headers={"Authentication": "test-trace-token"},
            projection=Projection(
                preset="agentarts",
                service_name="AgentArts.agent-1.default",
                attributes={
                    "gen_ai.resource.id": "agent-1",
                    "gen_ai.resource.type": "agent",
                    "domain.id": "domain-1",
                    "gen_ai.user.id": "user-1",
                },
            ),
        )
    )


def test_otlp_wire_carries_agentarts_shape(otlp_receiver):
    server, received = otlp_receiver
    exporter = _build_agentarts_exporter(
        f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
    )

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("wire-test")
    with tracer.start_as_current_span(
        "chat wire-model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "deepseek-v4.1-flash",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 200,
        },
    ):
        pass
    provider.shutdown()

    assert received, "OTLP HTTP request not received by mock receiver"
    request = received[0]
    # auth header (AgentArts platform convention: Authentication: <trace_token>)
    assert request["authentication"] == "test-trace-token"
    assert request["content_type"] == "application/x-protobuf"
    assert request["path"] == "/v1/traces"

    message = ExportTraceServiceRequest.FromString(request["body"])
    resource_spans = message.resource_spans
    assert len(resource_spans) == 1
    resource_attrs = {
        item.key: item.value.string_value
        for item in resource_spans[0].resource.attributes
    }
    assert resource_attrs["service.name"] == "AgentArts.agent-1.default"

    spans = resource_spans[0].scope_spans[0].spans
    assert len(spans) == 1
    attrs = {item.key: item for item in spans[0].attributes}
    assert attrs["gen_ai.span.kind"].value.string_value == "LLM"
    assert attrs["gen_ai.resource.id"].value.string_value == "agent-1"
    assert attrs["gen_ai.resource.type"].value.string_value == "agent"
    assert attrs["gen_ai.user.id"].value.string_value == "user-1"
    assert attrs["domain.id"].value.string_value == "domain-1"
    assert attrs["gen_ai.request.model"].value.string_value == "deepseek-v4.1-flash"
    assert attrs["gen_ai.usage.input_tokens"].value.int_value == 100
    assert attrs["gen_ai.usage.output_tokens"].value.int_value == 200
    # total_tokens filled in by the preset (platform-required)
    assert attrs["gen_ai.usage.total_tokens"].value.int_value == 300
