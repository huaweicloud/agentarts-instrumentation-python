# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentArts connectivity test script (mirrors the platform doc
"Report Trace Data" test_connection example).

Usage:
    1. Create an access entry in the AgentArts console
       (Observability & Optimization > Observability > Agent list >
       Access Agent) and copy the environment variable group issued
       by the access guide;
    2. Set the variables and run (official names, aligned 2026-11-20;
       legacy 0.1.x AGENTARTS_* names fall back):
         set agentarts_agentops_apm_token=...
         set agentarts_agentops_apm_exporter_endpoint=...   # e.g. https://apm-access.cn-southwest-2.myhuaweicloud.com
         set agent_resource_id=...
         set agent_domain_id=...          # optional, injects domain.id
         set agentarts_project_id=...     # optional, injects gen_ai.project.id
         set AGENTARTS_USER_ID=...        # optional
         python examples/report_to_agentarts.py
    3. Check the echo under the agent's trace analysis view in the
       AgentArts console.

This stage sends manually built static data, verifying only that the
endpoint, token and agent id match. For openjiuwen / jiuwenswarm
integration, feed the build_agentarts_exporter() result into
init_observability(..., span_exporter_override=...) instead
(design.md §6).
"""

from __future__ import annotations

import os

import agentarts_instrumentation.openjiuwen  # noqa: E402, F401 - registers the agentarts derivation

# Requires the workspace packages to be installed (uv sync, or
# pip install -e core -e openjiuwen).
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def _required_env(name: str, legacy: str = "") -> str:
    value = os.environ.get(name, "").strip() or os.environ.get(legacy, "").strip()
    if not value:
        raise SystemExit(
            f"Missing environment variable {name}; "
            "see the script docstring for access credentials"
        )
    return value


def build_agentarts_exporter(
    *,
    agent_id: str,
    trace_endpoint: str,
    trace_token: str,
) -> object:
    """Assemble the SDK exporter following the AgentArts access
    conventions."""
    return build_exporter(
        ExporterConfig(
            endpoint=trace_endpoint,
            # Measured (2026-11): the apm-access gateway only publishes
            # a gRPC channel (HTTP returns 404).
            protocol="grpc",
            # HTTP/2 metadata requires lowercase; the gateway parses
            # headers case-insensitively per HTTP semantics.
            headers={"authentication": trace_token},
            projection=Projection(
                preset="agentarts",
                service_name=f"AgentArts.{agent_id}.default",
                attributes={
                    "gen_ai.resource.id": agent_id,
                    "gen_ai.resource.type": "agent",
                },
            ),
        )
    )


def main() -> None:
    agent_id = _required_env("agent_resource_id", "AGENTARTS_AGENT_ID")
    agent_name = os.environ.get("AGENTARTS_AGENT_NAME", agent_id)
    trace_endpoint = _required_env(
        "agentarts_agentops_apm_exporter_endpoint", "AGENTARTS_TRACE_ENDPOINT"
    )
    trace_token = _required_env(
        "agentarts_agentops_apm_token", "AGENTARTS_TRACE_TOKEN"
    )
    domain_id = (
        os.environ.get("agent_domain_id", "").strip()
        or os.environ.get("AGENTARTS_DOMAIN_ID", "").strip()
    )
    project_id = os.environ.get("agentarts_project_id", "").strip()
    user_id = os.environ.get("AGENTARTS_USER_ID", "test_user")
    session_id = "test_session_12345"

    exporter = build_agentarts_exporter(
        agent_id=agent_id,
        trace_endpoint=trace_endpoint,
        trace_token=trace_token,
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agentarts-connectivity")

    common = {
        "gen_ai.resource.id": agent_id,
        "gen_ai.resource.type": "agent",
        "gen_ai.agent.name": agent_name,
        "gen_ai.conversation.id": session_id,
        "gen_ai.user.id": user_id,
    }
    if domain_id:
        common["domain.id"] = domain_id
    if project_id:
        common["gen_ai.project.id"] = project_id

    # AGENT root span
    with tracer.start_as_current_span("test_user_request") as root:
        for key, value in common.items():
            root.set_attribute(key, value)
        root.set_attribute("gen_ai.span.kind", "AGENT")
        root.set_attribute("gen_ai.operation.name", "invoke_agent")
        root.set_attribute(
            "gen_ai.input.messages",
            '[{"role":"user","parts":[{"type":"text","content":"test input"}]}]',
        )
        # LLM child span
        with tracer.start_as_current_span("test_llm_call") as model_span:
            for key, value in common.items():
                model_span.set_attribute(key, value)
            model_span.set_attribute("gen_ai.span.kind", "LLM")
            model_span.set_attribute("gen_ai.operation.name", "chat")
            model_span.set_attribute("gen_ai.request.model", "deepseek-v4.1-flash")
            model_span.set_attribute(
                "gen_ai.input.messages",
                '[{"role":"user","parts":[{"type":"text","content":"test input"}]}]',
            )
            model_span.set_attribute("gen_ai.response.model", "deepseek-v4.1-flash")
            model_span.set_attribute("gen_ai.response.finish_reasons", '["stop"]')
            model_span.set_attribute(
                "gen_ai.output.messages",
                '[{"role":"assistant","parts":[{"type":"text","content":"test output"}]}]',
            )
            model_span.set_attribute("gen_ai.usage.input_tokens", 100)
            model_span.set_attribute("gen_ai.usage.output_tokens", 200)
            model_span.set_attribute("gen_ai.usage.total_tokens", 300)
        root.set_attribute(
            "gen_ai.output.messages",
            '[{"role":"assistant","parts":[{"type":"text","content":"test output"}]}]',
        )
        root.set_attribute("gen_ai.usage.input_tokens", 100)
        root.set_attribute("gen_ai.usage.output_tokens", 200)
        root.set_attribute("gen_ai.usage.total_tokens", 300)

    provider.shutdown()
    print("Test trace data sent; check the AgentArts console trace-analysis view for the echo.")


if __name__ == "__main__":
    main()
