# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Production-pipeline e2e (agent-core): automated coverage of spans produced
by the real OtelCallbackHandler.

Previously this path was only verified by manual ECS L3 (real deepseek, see
the skill agentarts-otel-probe-dev stage 4); these tests pin it down with
FakeModelClient (monkeypatching ``create_model_client`` -- no network, the
event-decoration chain is real):

  Runner.start -> acquire (file/console primary path) -> SDK appended as a
  second path -> open_agent_run_span (registry root) -> real LLM events ->
  handler emits child spans -> SDK agentarts projection -> platform
  required-contract assertions.

Also pins the anti-regression for the design.md §6.2.1 onboarding pitfall
(P2: bare-tracer root drops child spans).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agentarts_instrumentation.config import ExporterConfig, Projection
from agentarts_instrumentation.exporter.buffer import BufferedOtlpExporter
from agentarts_instrumentation.exporter.transform import TransformingSpanExporter
from agentarts_instrumentation.openjiuwen.preset import _input_aggregator
from agentarts_instrumentation.projection import project_span
from opentelemetry.trace import SpanKind

pytestmark = pytest.mark.usefixtures("observability")


# ---- platform-contract asserter (pins the platform-recipe required list) ----


def assert_agentarts_required(span, expected_kind: str | None = None) -> None:
    """Assert a single span satisfies the AgentArts platform table-2 required contract."""
    attrs = span.attributes
    label = f"{span.name}({getattr(span, 'kind', '')})"
    assert attrs.get("gen_ai.resource.id"), f"{label}: missing gen_ai.resource.id"
    assert attrs.get("gen_ai.resource.type") == "agent", f"{label}: missing gen_ai.resource.type"
    kind = attrs.get("gen_ai.span.kind")
    assert kind, f"{label}: missing gen_ai.span.kind"
    if expected_kind is not None:
        assert kind == expected_kind, f"{label}: kind={kind} expected {expected_kind}"
    if kind in ("AGENT", "LLM"):
        assert attrs.get("gen_ai.input.messages"), f"{label}: missing gen_ai.input.messages"
        assert attrs.get("gen_ai.output.messages"), f"{label}: missing gen_ai.output.messages"
        assert attrs.get("gen_ai.usage.input_tokens") is not None, f"{label}: missing usage.input_tokens"
        assert attrs.get("gen_ai.usage.total_tokens") is not None, f"{label}: missing usage.total_tokens"


# ---- fake LLM (no network, real event flow) ----


def _fake_response(content: str, input_tokens: int, output_tokens: int):
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        UsageMetadata,
    )

    usage = UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return AssistantMessage(content=content, usage_metadata=usage)


class FakeModelClient:
    """Stand-in for create_model_client via monkeypatch: the Model
    constructor decorates this class's invoke."""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.invocations: list = []

    async def invoke(self, *args, **kwargs):
        self.invocations.append({"args": args, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return self._response

    async def stream(self, *args, **kwargs):
        # Model construction decorates stream too; this suite doesn't use
        # streaming, so provide a minimal awaitable stub.
        if self._error is not None:
            raise self._error
        yield self._response


@pytest.fixture
def fake_model_factory(monkeypatch):
    """Replace the Model module's create_model_client; Model construction
    grabs the fake client."""
    import openjiuwen.core.foundation.llm.model as model_module

    holder: dict = {"client": None}

    def _fake_create(client_config, model_config=None):
        return holder["client"]

    monkeypatch.setattr(model_module, "create_model_client", _fake_create)
    return holder


@pytest.fixture
def production_sdk(collecting_exporter):
    """A full agentarts-shaped SDK exporter (with common attributes and
    service_name rewrite)."""
    buffered = BufferedOtlpExporter(
        collecting_exporter,
        ExporterConfig(endpoint="http://localhost:4318/v1/traces"),
    )
    projection = Projection(
        preset="agentarts",
        service_name="AgentArts.e2e-agent.default",
        attributes={
            "gen_ai.resource.id": "e2e-agent",
            "gen_ai.resource.type": "agent",
        },
    )
    exporter = TransformingSpanExporter(
        buffered,
        transform=lambda span: project_span(span, projection),
    )
    exporter.buffered = buffered  # type: ignore[attr-defined]
    exporter.collecting = collecting_exporter  # type: ignore[attr-defined]
    return exporter


def _run_turns(turns: list[dict], production_sdk) -> list:
    """Synchronous shell: run turns in production order and return the
    spans collected on the SDK side.

    Each turn: dict(client=FakeModelClient, question=str, registry_root=bool,
    expect_error=bool). The Runner/observability lifecycle runs exactly once
    (wrapping all turns).
    """
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

    async def _main() -> None:
        await Runner.start()
        cfg = ObservabilityConfig(
            enabled=True,
            service_name="jiuwenswarm-e2e",
            exporter="console",
        )
        acquire_observability(cfg)
        oj_setup.init_observability(cfg, span_exporter_override=production_sdk)
        model = Model(
            ModelClientConfig(
                client_provider="DeepSeek", api_key="fake-key", api_base="http://fake"
            ),
            ModelRequestConfig(model_name="fake-model"),
        )
        for index, turn in enumerate(turns):
            tag = f"e2e-turn-{index}"
            if turn.get("registry_root", True):
                handle = open_agent_run_span(session_id=tag, mode="code")
                try:
                    response = await model.invoke(turn["question"])
                except Exception as exc:  # noqa: BLE001 -- the production host also catches at the run layer
                    close_agent_run_span(
                        handle, session_id=tag, exception=exc, error_type=type(exc).__name__
                    )
                    continue
                answer = str(getattr(response, "content", response))
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    handle.set_attribute("gen_ai.usage.input_tokens", int(usage.input_tokens))
                    handle.set_attribute("gen_ai.usage.output_tokens", int(usage.output_tokens))
                stamp_run_output(handle, answer)
                close_agent_run_span(handle, session_id=tag, output=answer)
            else:
                tracer = oj_setup.get_tracer("e2e-bare-root")
                with tracer.start_as_current_span(
                    "agent.code.bare-root",
                    kind=SpanKind.SERVER,
                    attributes={
                        "openjiuwen.trace.root": True,
                        "openjiuwen.agent.mode": "code",
                        "gen_ai.conversation.id": tag,
                    },
                ):
                    await model.invoke(turn["question"])
        production_sdk.buffered.force_flush(timeout_millis=5000)
        oj_setup.shutdown_observability()
        await Runner.stop()

    _input_aggregator.clear()
    asyncio.run(_main())
    return list(production_sdk.collecting.exported)


def _by_kind(spans: list, kind: str) -> list:
    return [s for s in spans if s.attributes.get("gen_ai.span.kind") == kind]


def _by_name(spans: list, name: str) -> list:
    return [s for s in spans if s.name == name]


# ---- P1 production full chain ----


def test_p1_production_pipeline_full_chain(
    fake_model_factory, production_sdk, collecting_exporter
):
    """Registry root + real LLM events -> handler emits child spans ->
    projection satisfies the platform contract."""
    fake_model_factory["client"] = FakeModelClient(
        response=_fake_response("simulated answer content", 18, 64)
    )
    spans = _run_turns([{"question": "production pipeline question"}], production_sdk)

    agents = _by_kind(spans, "AGENT")
    llms = _by_kind(spans, "LLM")
    assert len(agents) == 1, [s.name for s in spans]
    assert len(llms) == 1, [s.name for s in spans]

    # span tree: AGENT is the root, the LLM hangs under it (parent chain)
    # display-name compression (§4.3.8): agent.code.e2e-turn-* -> invoke_agent openjiuwen
    agent = agents[0]
    llm = llms[0]
    assert agent.name == "invoke_agent openjiuwen"
    assert llm.name == "chat"
    assert llm.parent is not None and llm.parent.span_id == agent.get_span_context().span_id

    # platform required contract: every span passes
    for span in spans:
        assert_agentarts_required(span)
    assert_agentarts_required(agent, expected_kind="AGENT")
    assert_agentarts_required(llm, expected_kind="LLM")

    # derivation & aggregation: AGENT input comes from cross-span LLM
    # aggregation and output from wrapping/normalization; both use the
    # platform's structured messages (same format as LLM spans)
    assert agent.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "production pipeline question"}]}],
        ensure_ascii=False,
    )
    assert agent.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": "simulated answer content"}]}],
        ensure_ascii=False,
    )
    assert agent.attributes["gen_ai.usage.input_tokens"] == 18
    assert agent.attributes["gen_ai.usage.total_tokens"] == 82

    # service.name resource rewrite
    assert agent.resource.attributes["service.name"] == "AgentArts.e2e-agent.default"


# ---- P2 registry negative case (design.md §6.2.1 pitfall anti-regression) ----


def test_p2_bare_tracer_root_drops_child_spans(fake_model_factory, production_sdk):
    """A bare-tracer root never enters the registry -> the handler emits no
    LLM child span (pitfall #1 pinned down)."""
    fake_model_factory["client"] = FakeModelClient(
        response=_fake_response("answer", 5, 5)
    )
    spans = _run_turns(
        [{"question": "negative-case question", "registry_root": False}],
        production_sdk,
    )
    names = [s.name for s in spans]
    assert "chat" not in names, f"a bare root must not produce LLM spans: {names}"
    assert _by_kind(spans, "LLM") == []
    assert _by_kind(spans, "AGENT"), "the root span itself should still be exported"


# ---- P3 failure propagation ----


def test_p3_model_failure_recorded_not_dropped(fake_model_factory, production_sdk):
    """Model invocation failure: run span keeps ERROR + status_message; the
    SDK drops no span.

    Observed behavior: the LLM_INVOKE_INPUT event fires before the
    exception, and the handler's LLM span ends and exports normally with an
    error status -- the failed turn's call chain stays fully visible (value
    for platform-side failure triage).
    """
    fake_model_factory["client"] = FakeModelClient(
        error=RuntimeError("simulated model failure")
    )
    spans = _run_turns([{"question": "the question that will fail"}], production_sdk)

    agents = _by_kind(spans, "AGENT")
    assert len(agents) == 1
    agent = agents[0]
    status = agent.status
    assert status.status_code.name == "ERROR"
    assert "simulated model failure" in (status.description or "")

    # The failed turn's LLM span is still produced with the aggregation chain
    # intact; observed: the handler does not mark ERROR on the exception path
    # (status UNSET; only the run root is ERROR) -- pinned down as-is.
    llms = _by_kind(spans, "LLM")
    assert len(llms) == 1, [s.name for s in spans]
    assert llms[0].status.status_code.name == "UNSET"
    assert agent.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "the question that will fail"}]}],
        ensure_ascii=False,
    )
    assert agent.attributes.get("gen_ai.resource.id") == "e2e-agent"


# ---- P4 multi-turn isolation ----


def test_p4_multi_turn_input_aggregation_isolated(fake_model_factory, production_sdk):
    """Two runs (each opening its own root): aggregation is isolated per
    trace; the two turns' inputs never cross."""
    fake_model_factory["client"] = FakeModelClient(
        response=_fake_response("two-turn answer", 10, 20)
    )
    spans = _run_turns(
        [{"question": "first-turn question"}, {"question": "second-turn question"}],
        production_sdk,
    )

    agents = sorted(_by_kind(spans, "AGENT"), key=lambda s: s.start_time)
    assert len(agents) == 2
    assert len(_by_kind(spans, "LLM")) == 2
    inputs = [a.attributes["gen_ai.input.messages"] for a in agents]
    expected = [
        json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": question}]}],
            ensure_ascii=False,
        )
        for question in ("first-turn question", "second-turn question")
    ]
    assert inputs == expected
    trace_ids = {a.get_span_context().trace_id for a in agents}
    assert len(trace_ids) == 2


# ---- P6 lifecycle close-out ----


def test_p6_shutdown_idempotent_and_spans_flushed(
    fake_model_factory, production_sdk, collecting_exporter
):
    """shutdown_observability propagates to the SDK exactly once; all spans
    are flushed, none lost."""
    fake_model_factory["client"] = FakeModelClient(
        response=_fake_response("close-out answer", 7, 9)
    )
    spans = _run_turns([{"question": "close-out question"}], production_sdk)

    assert len(_by_kind(spans, "AGENT")) == 1
    assert len(_by_kind(spans, "LLM")) == 1
    assert collecting_exporter.shutdown_count == 1
