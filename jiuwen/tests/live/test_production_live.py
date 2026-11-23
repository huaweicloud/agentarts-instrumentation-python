# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L4 production-pipeline live verification: agent-core production sequence
+ FakeModelClient -> real AgentArts.

Difference from P1 in tests/integration/test_production_pipeline.py: P1's
inner is a collecting fake (stays in-process); L4's inner is the **real
official gRPC exporter** -- spans actually reach the platform, asserted via
KooCLI echo. Only the LLM network call is mocked (FakeModelClient -- the
event-decoration chain is real); trace semantics match production exactly.

Real-network assertion focus: cross-span input aggregation (the AGENT's
input comes from the LLM span; the run span itself doesn't write it),
output rename, usage backfill, and the span tree's types.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter

QUESTION = "L4 production pipeline live question"
ANSWER = "L4 production pipeline simulated answer"
IN_TOKENS = 18
OUT_TOKENS = 64


class FakeModelClient:
    """Stand-in for create_model_client via monkeypatch -- Model construction
    decorates invoke/stream."""

    def __init__(self) -> None:
        self._response = self._build_response()

    @staticmethod
    def _build_response():
        from openjiuwen.core.foundation.llm.schema.message import (
            AssistantMessage,
            UsageMetadata,
        )

        return AssistantMessage(
            content=ANSWER,
            usage_metadata=UsageMetadata(
                input_tokens=IN_TOKENS,
                output_tokens=OUT_TOKENS,
                total_tokens=IN_TOKENS + OUT_TOKENS,
            ),
        )

    async def invoke(self, *args, **kwargs):
        return self._response

    async def stream(self, *args, **kwargs):
        yield self._response


@pytest.fixture()
def fake_model_factory(monkeypatch):
    import openjiuwen.core.foundation.llm.model as model_module

    holder: dict = {"client": None}

    def _fake_create(client_config, model_config=None):
        return holder["client"]

    monkeypatch.setattr(model_module, "create_model_client", _fake_create)
    return holder


def test_l4_production_pipeline_live(agentarts_env, platform_query, fake_model_factory):
    """L4: Runner -> acquire -> SDK append -> registry run span -> real LLM
    events -> real gRPC reporting -> platform echo.

    Builds its own exporter instance (doesn't reuse the session-scoped
    live_exporter -- L1's provider shutdown already propagated-close it, and
    SimpleSpanProcessor.shutdown semantics are as such).
    """
    fake_model_factory["client"] = FakeModelClient()
    session_id = f"live-l4-{time.strftime('%Y%m%d-%H%M%S')}"

    exporter = build_exporter(
        ExporterConfig(
            endpoint=agentarts_env["endpoint"],
            protocol="grpc",
            headers={"authentication": agentarts_env["token"]},
            projection=Projection(
                preset="agentarts",
                service_name=f"AgentArts.{agentarts_env['agent_id']}.default",
                attributes={
                    "gen_ai.resource.id": agentarts_env["agent_id"],
                    "gen_ai.resource.type": "agent",
                },
            ),
        )
    )

    async def _run_turn() -> None:
        from openjiuwen.core.foundation.llm.model import Model
        from openjiuwen.core.foundation.llm.schema.config import (
            ModelClientConfig,
            ModelRequestConfig,
        )
        from openjiuwen.core.runner.runner import Runner
        from openjiuwen.extensions.observability import setup as oj_setup
        from openjiuwen.extensions.observability.config import ObservabilityConfig
        from openjiuwen.harness.observability import acquire_observability
        from openjiuwen.harness.observability.run_span import (
            close_agent_run_span,
            open_agent_run_span,
            stamp_run_output,
        )

        await Runner.start()
        cfg = ObservabilityConfig(
            enabled=True,
            service_name="jiuwenswarm-live-l4",
            exporter="console",
        )
        acquire_observability(cfg)
        oj_setup.init_observability(cfg, span_exporter_override=exporter)

        model = Model(
            ModelClientConfig(
                client_provider="DeepSeek",
                api_key="fake-key",
                api_base="http://fake",
            ),
            ModelRequestConfig(model_name="fake-model"),
        )
        handle = open_agent_run_span(session_id=session_id, mode="code")
        response = await model.invoke(QUESTION)
        answer = str(getattr(response, "content", response))
        usage = getattr(response, "usage_metadata", None)
        if handle is not None and usage is not None:
            handle.set_attribute("gen_ai.usage.input_tokens", int(usage.input_tokens))
            handle.set_attribute("gen_ai.usage.output_tokens", int(usage.output_tokens))
        stamp_run_output(handle, answer)
        close_agent_run_span(handle, session_id=session_id, output=answer)

        live_export_flushed = exporter.force_flush(timeout_millis=20000)
        assert live_export_flushed, "SDK-side flush failed"
        oj_setup.shutdown_observability()
        await Runner.stop()

    asyncio.run(_run_turn())

    # platform echo: AGENT + LLM (+ context.window.commit) form the tree
    spans = platform_query["wait_for_echo_tree"](session_id, min_spans=3)
    kinds = {span.get("span_type") for span in spans}
    assert {"AGENT", "LLM"}.issubset(kinds), [s.get("span_type") for s in spans]

    agent = next(s for s in spans if s.get("span_type") == "AGENT")
    llm = next(s for s in spans if s.get("span_type") == "LLM")
    assert llm["trace_id"] == agent["trace_id"]

    # cross-span aggregation: the AGENT's input comes from the LLM span (the
    # run span itself doesn't write it)
    assert agent["input"] == QUESTION
    # rename: stamp_run_output writes openjiuwen.span.output -> gen_ai.output.messages
    assert agent["output"] == ANSWER
    # real usage backfill + total filled in
    assert agent["input_tokens"] == IN_TOKENS
    assert agent["tokens"] == IN_TOKENS + OUT_TOKENS
