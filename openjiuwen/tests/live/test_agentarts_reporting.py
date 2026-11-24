# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real AgentArts reporting E2E cases (design.md §8, live group).

Case matrix:
- L1 static full-chain roundtrip: in standalone-SDK-provider shape, report a
  three-span AGENT/LLM/TOOL(skill) tree; poll the KooCLI echo to assert the
  tree/types/in/out/tokens, and ShowOpsTrace for the skill field;
- L2 auth negative: a bad token -> official exporter FAILURE -> SDK warning
  visible (real-network verification of exporter.py's return-value checks);
- L3 routing negative: empty service_name -> gateway 504 -> warning visible
  (real-network pinning of an onboarding-recipe pitfall);
- L4 production pipeline (agent-core + FakeModelClient; needs an agent-core
  environment, ECS recommended);
- L5 real LLM (needs DEEPSEEK_API_KEY and a Linux/unblocked environment).
"""

from __future__ import annotations

import logging
import time

import pytest
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


@pytest.fixture()
def log_captor():
    """Capture exporter warning logs (negative cases assert failure visibility)."""

    class _Captor(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    target = logging.getLogger("agentarts_instrumentation.exporter.buffer")
    captor = _Captor()
    target.addHandler(captor)
    try:
        yield captor
    finally:
        target.removeHandler(captor)


def _emit_static_tree(exporter, session_id: str, *, with_skill: bool = True) -> dict:
    """Report a three-span static tree in standalone-SDK shape (own provider
    + SimpleSpanProcessor).

    trace_id is generated randomly by the provider and taken from the
    platform response during echo verification. Returns local expectations.
    """
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("live-e2e")

    with tracer.start_as_current_span(
        f"agent.code.{session_id}",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "openjiuwen.agent.mode": "code",
            "gen_ai.conversation.id": session_id,
            "gen_ai.input.messages": "static-case user input",
            "gen_ai.output.messages": "static-case final answer",
            "gen_ai.usage.input_tokens": 11,
            "gen_ai.usage.output_tokens": 22,
        },
    ):
        tracer.start_span(
            "chat deepseek-static",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.input.messages": '[{"role": "user", "content": "static-case user input"}]',
                "gen_ai.output.messages": '[{"role": "assistant", "content": "static-case answer"}]',
                "gen_ai.usage.input_tokens": 11,
                "gen_ai.usage.output_tokens": 22,
            },
        ).end()
        if with_skill:
            tracer.start_span(
                "execute_tool read_file",
                attributes={
                    "gen_ai.tool.name": "read_file",
                    "gen_ai.tool.call.arguments": '{"path": ".agents/skills/jw-coding-flow/SKILL.md"}',
                },
            ).end()
    provider.shutdown()
    return {
        "input_tokens": 11,
        "output_tokens": 22,
        "total": 33,
        "skill_name": "jw-coding-flow",
    }


def test_l1_static_tree_roundtrip(live_exporter, agentarts_env, platform_query):
    """L1: three-span static tree reported -> the platform echoes
    tree/types/tokens/in-out; the skill field is checked closely via
    ShowOpsTrace."""
    session_id = f"live-l1-{time.strftime('%Y%m%d-%H%M%S')}"
    expected = _emit_static_tree(live_exporter, session_id)

    spans = platform_query["wait_for_echo_tree"](session_id, min_spans=3)
    kinds = {span.get("span_type") for span in spans}
    assert {"AGENT", "LLM", "TOOL"}.issubset(kinds), [s.get("span_type") for s in spans]

    agent = next(s for s in spans if s.get("span_type") == "AGENT")
    assert agent["input_tokens"] == expected["input_tokens"]
    assert agent["tokens"] == expected["total"]
    assert "static-case user input" in agent["input"]
    assert "static-case final answer" in agent["output"]

    detail = platform_query["query_trace_detail"](agent["trace_id"])
    assert expected["skill_name"] in str(detail), f"ShowOpsTrace did not echo skill info: {str(detail)[:200]}"


def test_l2_bad_token_failure_visible(agentarts_env, log_captor, make_span):
    """L2: bad token -> FAILURE is not silent -- the SDK warning is visible
    (real-network verification of the return-value checks)."""
    bad = build_exporter(
        ExporterConfig(
            endpoint=agentarts_env["endpoint"],
            protocol="grpc",
            headers={"authentication": "definitely-invalid-token"},
            projection=Projection(
                preset="agentarts",
                service_name=f"AgentArts.{agentarts_env['agent_id']}.default",
            ),
        )
    )
    try:
        bad.export([make_span(name="l2-probe")])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
            "batch export returned" in m for m in log_captor.messages
        ):
            time.sleep(0.5)
    finally:
        bad.shutdown()
    assert any("batch export returned" in m for m in log_captor.messages), log_captor.messages


def test_l3_missing_service_name_route_failure_visible(agentarts_env, log_captor, make_span):
    """L3: empty service_name -> gateway routing failure (504) -- the SDK
    warning is visible (real-network pinning of an onboarding-recipe
    pitfall)."""
    no_name = build_exporter(
        ExporterConfig(
            endpoint=agentarts_env["endpoint"],
            protocol="grpc",
            headers={"authentication": agentarts_env["token"]},
            projection=Projection(preset="agentarts"),  # service_name intentionally unset
        )
    )
    try:
        no_name.export([make_span(name="l3-probe")])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
            "batch export returned" in m for m in log_captor.messages
        ):
            time.sleep(0.5)
    finally:
        no_name.shutdown()
    assert any("batch export returned" in m for m in log_captor.messages), log_captor.messages
