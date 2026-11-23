# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Projection semantics unit tests (design.md §4.2/§4.3)."""

from __future__ import annotations

import json

import pytest
from agentarts_instrumentation.config import Projection
from agentarts_instrumentation.jiuwen.preset import (
    _error_aggregator,
    _input_aggregator,
    _parent_refs,
    _round_counter,
    _token_aggregator,
)
from agentarts_instrumentation.projection import project_span
from opentelemetry.sdk.trace import Event
from opentelemetry.trace import Status, StatusCode


@pytest.fixture(autouse=True)
def _reset_input_aggregator():
    """Cross-span aggregators are module-level singletons and make_span uses a
    fixed trace_id -- clear per test to prevent leakage."""
    _input_aggregator.clear()
    _token_aggregator.clear()
    _error_aggregator.clear()
    _parent_refs.clear()
    _round_counter.clear()
    yield
    _input_aggregator.clear()
    _token_aggregator.clear()
    _error_aggregator.clear()
    _parent_refs.clear()
    _round_counter.clear()


def _tool_span(make_span, arguments: dict, extra_attrs: dict | None = None):
    attrs = {
        "gen_ai.tool.name": "read_file",
        "gen_ai.tool.call.arguments": json.dumps(arguments),
    }
    attrs.update(extra_attrs or {})
    return make_span("execute_tool read_file", attributes=attrs)


# ---- general rules ----


def test_identity_projection_returns_same_object(make_span):
    span = make_span(attributes={"a": 1})
    assert project_span(span, Projection()) is span


def test_attributes_inject_without_override(make_span):
    span = make_span(attributes={"gen_ai.resource.id": "existing"})
    out = project_span(
        span,
        Projection(attributes={"gen_ai.resource.id": "injected", "domain.id": "d1"}),
    )
    assert out.attributes["gen_ai.resource.id"] == "existing"
    assert out.attributes["domain.id"] == "d1"
    assert span.attributes["gen_ai.resource.id"] == "existing"


def test_drop_exact_and_prefix(make_span):
    span = make_span(
        attributes={
            "gen_ai.usage.total_tokens": 1,
            "openjiuwen.turn.number": 2,
            "keep.me": 3,
        }
    )
    out = project_span(span, Projection(drop=["gen_ai.usage.total_tokens", "openjiuwen.*"]))
    assert "gen_ai.usage.total_tokens" not in out.attributes
    assert "openjiuwen.turn.number" not in out.attributes
    assert out.attributes["keep.me"] == 3


def test_truncate_only_strings(make_span):
    span = make_span(attributes={"s": "abcdefg", "n": 123456, "l": ["x", "y"]})
    out = project_span(span, Projection(truncate=5))
    assert out.attributes["s"] == "abcde"
    assert out.attributes["n"] == 123456
    assert out.attributes["l"] == ["x", "y"]


def test_user_rename_without_override(make_span):
    span = make_span(attributes={"a.b": 1, "c.d": 9})
    out = project_span(span, Projection(rename={"a.b": "c.d"}))
    assert out.attributes["c.d"] == 9
    assert out.attributes["a.b"] == 1  # target exists: source kept, not overridden


def test_user_rename_applies(make_span):
    span = make_span(attributes={"a.b": 1})
    out = project_span(span, Projection(rename={"a.b": "c.d"}))
    assert out.attributes == {"c.d": 1}


# ---- service.name rewrite (AgentArts onboarding convention) ----


def test_service_name_rewrite_keeps_other_resource_attrs(make_span):
    from agentarts_instrumentation.exporter.transform import clone_readable_span
    from opentelemetry.sdk.resources import Resource

    base = make_span(attributes={"a": 1})
    span = clone_readable_span(
        base,
        {"a": 1},
        resource=Resource.create({"service.name": "jiuwenswarm", "custom.attr": "x"}),
    )
    out = project_span(span, Projection(service_name="AgentArts.agent-1.default"))
    assert out.resource.attributes["service.name"] == "AgentArts.agent-1.default"
    assert out.resource.attributes["custom.attr"] == "x"
    assert out.attributes == {"a": 1}
    # original span's resource untouched
    assert span.resource.attributes["service.name"] == "jiuwenswarm"


def test_service_name_blank_keeps_original_resource(make_span):
    from agentarts_instrumentation.exporter.transform import clone_readable_span
    from opentelemetry.sdk.resources import Resource

    base = make_span(attributes={"a": 1})
    span = clone_readable_span(
        base, {"a": 1}, resource=Resource.create({"service.name": "jiuwenswarm"})
    )
    out = project_span(span, Projection(drop=["a"]))
    assert out.resource is span.resource


# ---- AgentArts preset: span.kind derivation ----


def test_agentarts_llm_kind_and_total_tokens(make_span):
    span = make_span(
        "chat maas-DeepSeek-V4.1-Flash",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "maas-DeepSeek-V4.1-Flash",
            "gen_ai.request.stream": True,
            "gen_ai.usage.input_tokens": 145,
            "gen_ai.usage.output_tokens": 23,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    attrs = out.attributes
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.usage.total_tokens"] == 168
    assert attrs["gen_ai.request.is_stream"] is True
    assert "gen_ai.request.stream" not in attrs
    # original span untouched
    assert span.attributes["gen_ai.request.stream"] is True
    assert "gen_ai.usage.total_tokens" not in span.attributes


def test_agentarts_agent_cache_tokens_aggregated(make_span):
    """AGENT root aggregates cache_read/cache_creation across the trace's LLM
    spans (keys named like the host semconv; the host only writes LLM spans,
    the root is aggregated by the probe); never overrides host-written
    values."""
    llm_specs = [
        (0x1122334455667791, {"gen_ai.usage.cache_read.input_tokens": 42000,
                              "gen_ai.usage.cache_creation.input_tokens": 1326}),
        (0x1122334455667792, {"gen_ai.usage.cache_read.input_tokens": 8000,
                              "gen_ai.usage.cache_creation.input_tokens": 0,
                              "gen_ai.usage.input_tokens": 500,
                              "gen_ai.usage.output_tokens": 20}),
    ]
    for span_id, extra in llm_specs:
        attrs = {"gen_ai.operation.name": "chat"}
        attrs.update(extra)
        project_span(
            make_span("llm.call", attributes=attrs, span_id=span_id),
            Projection(preset="agentarts"),
        )
    root = make_span(
        "agent.code.normal.sid",
        attributes={"openjiuwen.trace.root": True},
        span_id=0x1122334455667793,
    )
    out = project_span(root, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.usage.cache_read.input_tokens"] == 50000
    assert out.attributes["gen_ai.usage.cache_creation.input_tokens"] == 1326
    # existing aggregation semantics unaffected: input/output still accumulate
    assert out.attributes["gen_ai.usage.input_tokens"] == 500
    assert out.attributes["gen_ai.usage.output_tokens"] == 20

    # When the host already wrote cache keys on the root, keep them (no override)
    root_written = make_span(
        "agent.code.normal.sid2",
        attributes={
            "openjiuwen.trace.root": True,
            "gen_ai.usage.cache_read.input_tokens": 7,
        },
        span_id=0x1122334455667794,
    )
    project_span(
        make_span(
            "llm.call",
            attributes={"gen_ai.operation.name": "chat",
                        "gen_ai.usage.cache_read.input_tokens": 100},
            span_id=0x1122334455667795,
        ),
        Projection(preset="agentarts"),
    )
    out2 = project_span(root_written, Projection(preset="agentarts"))
    assert out2.attributes["gen_ai.usage.cache_read.input_tokens"] == 7


def test_agentarts_run_root_agent_kind_name_and_total(make_span):
    span = make_span(
        "agent.code.normal.sid-123",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.agent.mode": "code.normal",
            "gen_ai.conversation.id": "session-1",
            "gen_ai.usage.input_tokens": 1280,
            "gen_ai.usage.output_tokens": 356,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    attrs = out.attributes
    assert attrs["gen_ai.span.kind"] == "AGENT"
    assert attrs["gen_ai.agent.name"] == "code.normal"
    assert attrs["gen_ai.usage.total_tokens"] == 1636


def test_agentarts_agent_mode_missing_keeps_silent(make_span):
    span = make_span("agent.x.sid", attributes={"openjiuwen.trace.root": True})
    out = project_span(span, Projection(preset="agentarts"))
    assert "gen_ai.agent.name" not in out.attributes


# ---- AgentArts preset: child-span error propagation (§4.3.9) ----


_ROOT_SPAN_ID = 0x0100000000000001
_CHILD_SPAN_ID = 0x0100000000000002
_CHILD_SPAN_ID_B = 0x0100000000000003
_CHILD_SPAN_ID_C = 0x0100000000000004


def _agent_root(make_span, status=None):
    return make_span(
        "agent.code.normal.sid-123",
        attributes={"openjiuwen.trace.root": True},
        span_id=_ROOT_SPAN_ID,
        status=status,
    )


def _failed_tool(make_span, span_id=_CHILD_SPAN_ID, name="execute_tool shell"):
    return make_span(
        name,
        attributes={"gen_ai.tool.name": name.split()[-1]},
        span_id=span_id,
        parent_span_id=_ROOT_SPAN_ID,
        status=Status(StatusCode.ERROR, "exit code 1"),
    )


def _ok_llm_span(make_span, span_id):
    """A successful LLM child span (the host's success path explicitly calls
    set_status(OK), callback L1199)."""
    return make_span(
        "llm.call",
        attributes={"gen_ai.operation.name": "chat"},
        span_id=span_id,
        parent_span_id=_ROOT_SPAN_ID,
        status=Status(StatusCode.OK),
    )


def test_agentarts_child_error_propagates_to_root_status(make_span):
    """Terminal failure (no successful business child span after the last
    failure) -> root escalates to ERROR -- a fix for the host not propagating
    child failures, which distorted agent status on the platform (observed
    2026-11-20)."""
    failed = project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    root = _agent_root(make_span)
    out = project_span(root, Projection(preset="agentarts"))
    assert out.status.status_code == StatusCode.ERROR
    assert out.status.description == "child span failed: execute_tool shell"
    # original span untouched (the local trajectory store still receives the
    # host's original status)
    assert root.status is None or root.status.status_code != StatusCode.ERROR
    # the failed child span's own status passes through unchanged
    assert failed.status.status_code == StatusCode.ERROR


def test_agentarts_root_status_unchanged_without_child_errors(make_span):
    """With no failed child spans the root status stays as-is (default
    None/UNSET); never fabricates an error."""
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status is None or out.status.status_code == StatusCode.UNSET


def test_agentarts_root_own_error_not_overridden(make_span):
    """Root itself already ERROR (host judged the run failed): keep the
    original status and description, don't override."""
    root = _agent_root(make_span, status=Status(StatusCode.ERROR, "agent aborted"))
    project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    out = project_span(root, Projection(preset="agentarts"))
    assert out.status.status_code == StatusCode.ERROR
    assert out.status.description == "agent aborted"


def test_agentarts_dropped_span_errors_do_not_propagate(make_span):
    """Internal bookkeeping spans (context.window.commit) are neither
    reported nor propagated -- the whole span was already filtered out by
    DROP_SPANS, and registration happens after filtering, preventing false
    failures for the whole run."""
    project_span(
        make_span(
            "context.window.commit",
            span_id=_CHILD_SPAN_ID,
            parent_span_id=_ROOT_SPAN_ID,
            status=Status(StatusCode.ERROR, "compress failed"),
        ),
        Projection(preset="agentarts"),
    )
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status is None or out.status.status_code != StatusCode.ERROR


def test_agentarts_multiple_child_errors_last_name_in_description(make_span):
    """Multiple failed child spans (no recovery): the description points to
    the **last** failure name -- under terminal semantics the last failure is
    the run's terminator (plan B, revised 2026-11-21)."""
    project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    project_span(
        _failed_tool(make_span, span_id=_CHILD_SPAN_ID_B, name="execute_tool search"),
        Projection(preset="agentarts"),
    )
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status.status_code == StatusCode.ERROR
    assert out.status.description == "child span failed: execute_tool search"


def test_agentarts_recovered_error_not_escalated(make_span):
    """Core plan-B scenario (observed on trace c2acd7**[redacted]**): a
    failure followed by a successful business span means recovery -> the
    root status stays untouched (run completeness) and gen_ai.error.recovered
    leaves a trace."""
    project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    ok = project_span(
        _ok_llm_span(make_span, span_id=_CHILD_SPAN_ID_B),
        Projection(preset="agentarts"),
    )
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status is None or out.status.status_code != StatusCode.ERROR
    assert out.attributes["gen_ai.error.recovered"] == "execute_tool shell"
    # successful child span's OK status passes through unchanged
    assert ok.status.status_code == StatusCode.OK


def test_agentarts_step_none_is_not_recovery_signal(make_span):
    """A STEP span with status=None is not a recovery signal -- after a tool
    failure, the turn's closing steps (all STEPs are None) must not be
    misjudged as self-healing; the root still escalates to ERROR."""
    project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    project_span(
        make_span(
            "agent.demo.react_iteration.1",
            attributes={"openjiuwen.trajectory.record.kind": "step"},
            span_id=_CHILD_SPAN_ID_B,
            parent_span_id=_ROOT_SPAN_ID,
            status=None,
        ),
        Projection(preset="agentarts"),
    )
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status.status_code == StatusCode.ERROR


def test_agentarts_last_failure_after_recovery_escalates(make_span):
    """Failure after recovery with no later recovery: terminality counts the
    last failure; root ERROR points at the last failure's name."""
    project_span(_failed_tool(make_span), Projection(preset="agentarts"))
    project_span(
        _ok_llm_span(make_span, span_id=_CHILD_SPAN_ID_B),
        Projection(preset="agentarts"),
    )
    project_span(
        _failed_tool(
            make_span, span_id=_CHILD_SPAN_ID_C, name="execute_tool search"
        ),
        Projection(preset="agentarts"),
    )
    out = project_span(_agent_root(make_span), Projection(preset="agentarts"))
    assert out.status.status_code == StatusCode.ERROR
    assert out.status.description == "child span failed: execute_tool search"


def test_agentarts_error_propagation_bounded(make_span):
    """The registry is bounded: capacity evicts the oldest trace (same
    pattern as the token/input aggregators)."""
    for index in range(_error_aggregator._capacity + 2):
        _error_aggregator.record(index, f"span-{index}", is_error=True)
    assert _error_aggregator.take(0) is None
    latest = _error_aggregator.take(_error_aggregator._capacity + 1)
    assert latest == [f"span-{_error_aggregator._capacity + 1}", False]


def test_agentarts_tool_kind(make_span):
    span = make_span("execute_tool calculator", attributes={"gen_ai.tool.name": "calculator"})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.span.kind"] == "TOOL"


def test_agentarts_step_kind_round_renumbered_per_trace(make_span):
    """react rounds renumber per trace from 1: leftover host counters (span
    names starting at .9 after session resume or checkpoint recovery) don't
    affect the platform's round -- projection counts in order."""
    span = make_span(
        "agent.demo.react_iteration.9",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.span.kind"] == "STEP"
    # Within one trace the first step is round 1; the display name consumes
    # the renumbered value too
    assert out.attributes["gen_ai.react.round"] == 1
    assert out.name == "react step"
    # second step in the same trace (host .10) -> round 2
    span_b = make_span(
        "agent.demo.react_iteration.10",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=0x1122334455667790,
    )
    out_b = project_span(span_b, Projection(preset="agentarts"))
    assert out_b.attributes["gen_ai.react.round"] == 2
    assert out_b.name == "react step"


def test_agentarts_step_round_restarts_on_new_trace(make_span):
    """Rounds restart from scratch on a new trace; projecting the AGENT root
    (trace close-out) also releases the counter."""
    other_trace = 0x99887766554433221100FFEEDDCCBBAA
    first = make_span(
        "agent.demo.react_iteration.3",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        trace_id=other_trace,
    )
    out = project_span(first, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.react.round"] == 1
    second = make_span(
        "agent.demo.react_iteration.4",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=0x1122334455667791,
        trace_id=other_trace,
    )
    out = project_span(second, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.react.round"] == 2
    # AGENT root close-out clears the trace's counter -> a later step on the
    # same trace_id starts from 1 again
    root = make_span(
        "agent.demo.invoke",
        attributes={"openjiuwen.trace.root": True},
        trace_id=other_trace,
    )
    assert project_span(root, Projection(preset="agentarts")) is not None
    third = make_span(
        "agent.demo.react_iteration.5",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=0x1122334455667792,
        trace_id=other_trace,
    )
    out = project_span(third, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.react.round"] == 1


def test_agentarts_step_round_falls_back_to_turn_number(make_span):
    span = make_span(
        "agent.demo.react",
        attributes={
            "openjiuwen.trajectory.record.kind": "step",
            "openjiuwen.turn.number": 4,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.react.round"] == 4


def test_agentarts_team_workflow_kind(make_span):
    span = make_span("team.alpha")
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.span.kind"] == "WORKFLOW"


def test_agentarts_existing_span_kind_preserved(make_span):
    span = make_span(
        "custom",
        attributes={"gen_ai.span.kind": "CUSTOM"},
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.span.kind"] == "CUSTOM"


def test_agentarts_no_signature_no_kind(make_span):
    # Use a neutral name to verify "no signature -> no kind written";
    # context.window.commit is already trimmed as a whole span
    span = make_span("internal.noise", attributes={"x": 1})
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert "gen_ai.span.kind" not in out.attributes


# ---- AgentArts preset: key-name fixes ----


def test_agentarts_builtin_renames(make_span):
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.cache_write.input_tokens": 100,
            "openjiuwen.gen_ai.response.time_to_first_token_ms": 128,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    attrs = out.attributes
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 100
    assert "gen_ai.usage.cache_write.input_tokens" not in attrs
    assert attrs["gen_ai.response.time_to_first_token"] == 128
    assert "openjiuwen.gen_ai.response.time_to_first_token_ms" not in attrs


def test_agentarts_rename_target_existing_not_overridden(make_span):
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.cache_write.input_tokens": 100,
            "gen_ai.usage.cache_creation.input_tokens": 45,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.usage.cache_creation.input_tokens"] == 45
    assert out.attributes["gen_ai.usage.cache_write.input_tokens"] == 100


def test_agentarts_run_span_output_renamed(make_span):
    """span.output (plain text) -> wrapped as structured output.messages
    (same format as LLM spans)."""
    span = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "openjiuwen.agent.mode": "code",
            "openjiuwen.span.output": "final answer text",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert out.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": "final answer text"}]}],
        ensure_ascii=False,
    )
    assert "openjiuwen.span.output" not in out.attributes


def test_agentarts_drops_context_window_commit_span(make_span):
    """Trajectory-protocol-specific spans (context.window.commit) are not
    reported: projection returns None."""
    span = make_span(
        "context.window.commit",
        attributes={
            "openjiuwen.trajectory.record.kind": "event",
            "openjiuwen.trajectory.payload": '{"messages": ["..."]}',
        },
    )
    assert project_span(span, Projection(preset="agentarts")) is None
    # non-preset projection unaffected
    assert project_span(span, Projection()) is span


def test_agentarts_drops_framework_namespaces(make_span):
    """The preset trims whole host-framework namespaces (after derivation);
    gen_ai.* and standard OTel keys are kept."""
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": "sess-1",
            "otel.scope.name": "openjiuwen.harness.observability",
            "openjiuwen.execution.subject.id": "main",
            "openjiuwen.trajectory.schema_version": 2,
            "openjiuwen.inference.id": "abc",
            "openjiuwen.request.purpose": "assistant",
            "openjiuwen.trajectory.record.kind": "inference",  # derivation source: trimmed after use
            "openjiuwen.gen_ai.response.total_latency_ms": 12.5,
            "langfuse.observation.input": "hello",
            "langfuse.observation.output": "reply",
            "langfuse.observation.type": "generation",
            "agentteam.session.id": "team-1",
            "deepagent.task.iteration": 1,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    for key in (
        "openjiuwen.execution.subject.id",
        "openjiuwen.trajectory.schema_version",
        "openjiuwen.inference.id",
        "openjiuwen.request.purpose",
        "openjiuwen.trajectory.record.kind",
        "openjiuwen.gen_ai.response.total_latency_ms",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.observation.type",
        "agentteam.session.id",
        "deepagent.task.iteration",
    ):
        assert key not in out.attributes, key
    # platform semantics and standard OTel keys kept
    assert out.attributes["gen_ai.operation.name"] == "chat"
    assert out.attributes["gen_ai.conversation.id"] == "sess-1"
    assert out.attributes["otel.scope.name"] == "openjiuwen.harness.observability"
    # kind was derived before trimming
    assert out.attributes["gen_ai.span.kind"] == "LLM"
    # original span untouched (local trajectory store keeps everything)
    assert "openjiuwen.request.purpose" in span.attributes


def test_agentarts_tool_input_output_dropped(make_span):
    """On TOOL spans gen_ai.tool.input/output duplicate call.arguments/result
    (measured: tool.input is an [args + metadata] wrapper and tool.output
    equals call.result), so they are trimmed."""
    span = make_span(
        "execute_tool bash",
        attributes={
            "gen_ai.tool.name": "bash",
            "gen_ai.tool.input": '[[{"command": "uptime"}], {"session": "s1", "tool_call_id": "c1"}]',
            "gen_ai.tool.call.arguments": '{"command": "uptime"}',
            "gen_ai.tool.output": " 14:20  load average: 0.05",
            "gen_ai.tool.call.result": " 14:20  load average: 0.05",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    attrs = out.attributes
    assert "gen_ai.tool.input" not in attrs
    assert "gen_ai.tool.output" not in attrs
    assert attrs["gen_ai.tool.call.arguments"] == '{"command": "uptime"}'
    assert attrs["gen_ai.tool.call.result"] == " 14:20  load average: 0.05"


def test_agentarts_llm_structured_output_not_overridden_by_rename(make_span):
    """When an LLM span already has structured gen_ai.output.messages, rename
    doesn't override it (target-exists semantics)."""
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.output.messages": '[{"role": "assistant", "content": "structured answer"}]',
            "openjiuwen.span.output": "plain text answer",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.output.messages"].startswith('[{"role"')
    # after namespace trimming the source key is not reported
    assert "openjiuwen.span.output" not in out.attributes


def test_agentarts_turn_input_aggregated_from_llm(make_span):
    """LLM spans projected first record their input; the AGENT root projected
    later gets gen_ai.input.messages filled in."""
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": "user input for this turn"}]}]
            ),
        },
    )
    project_span(llm, Projection(preset="agentarts"))  # record

    agent = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
        },
    )
    out = project_span(agent, Projection(preset="agentarts"))  # take
    # input is wrapped in the platform structure too (same format as LLM
    # spans); the first user message is kept verbatim
    assert out.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "user input for this turn"}]}],
        ensure_ascii=False,
    )


def test_agentarts_agent_input_present_not_overridden(make_span):
    """AGENT root already having gen_ai.input.messages (written by the host)
    is not overridden."""
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": "from LLM"}]}]
            ),
        },
    )
    project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "gen_ai.input.messages": "input written by the host",
        },
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.input.messages"] == "input written by the host"


def test_agentarts_agent_input_cache_miss_leaves_unset(make_span):
    """On cache miss (no earlier LLM span) the input key is left unset."""
    agent = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
        },
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert "gen_ai.input.messages" not in out.attributes


def test_agentarts_turn_input_first_write_wins(make_span):
    """When multiple LLM spans in one trace are projected, the earliest
    recorded input wins (multi-turn history shares the same first message)."""
    for text in ("first turn input", "second turn input"):
        llm = make_span(
            "chat model",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "parts": [{"type": "text", "content": text}]}]
                ),
            },
        )
        project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "first turn input"}]}],
        ensure_ascii=False,
    )


def test_agentarts_turn_input_skips_reminder_takes_last_user(make_span):
    """Observed shape in resumed chats (revised 2026-11-19): the first user
    message is host-injected <system-reminder> dynamic context, while the
    real input sits at the end wrapped in a '你收到一条消息：{json}' envelope --
    AGENT input must be the unwrapped user text."""
    envelope = (
        "你收到一条消息：\n"
        + json.dumps(
            {
                "source": "web",
                "content": "Analyze the directory structure E:\\code\\jiuwen",
                "origin_kind": "external_user_authored",
            },
            ensure_ascii=False,
        )
    )
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "type": "text",
                                "content": "<system-reminder>dynamic runtime context, "
                                "current model deepseek-v4-flash</system-reminder>",
                            }
                        ],
                    },
                    {"role": "user", "parts": [{"type": "text", "content": envelope}]},
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "content": "I will analyze the directory structure."}],
                    },
                ],
                ensure_ascii=False,
            ),
        },
    )
    project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "Analyze the directory structure E:\\code\\jiuwen"}]}],
        ensure_ascii=False,
    )


def test_agentarts_turn_input_reminder_only_not_recorded(make_span):
    """Only system-reminder injected messages (no real user input): the turn
    input is not recorded."""
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [
                            {"type": "text", "content": "<system-reminder>injected context only</system-reminder>"}
                        ],
                    }
                ]
            ),
        },
    )
    project_span(llm, Projection(preset="agentarts"))
    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert "gen_ai.input.messages" not in out.attributes


def test_agentarts_turn_input_envelope_json_damaged_keeps_original(make_span):
    """Damaged envelope JSON keeps the original (input is not lost because
    unwrapping failed)."""
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [{"type": "text", "content": "你收到一条消息：{broken JSON"}],
                    }
                ]
            ),
        },
    )
    project_span(llm, Projection(preset="agentarts"))
    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.input.messages"] == json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": "你收到一条消息：{broken JSON"}]}],
        ensure_ascii=False,
    )


def test_agentarts_agent_tokens_accumulated_from_llms(make_span):
    """The AGENT root's usage is the token sum over the trace's LLM spans."""
    for in_tok, out_tok in ((100, 20), (355, 811)):
        llm = make_span(
            "chat model",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": in_tok,
                "gen_ai.usage.output_tokens": out_tok,
            },
        )
        project_span(llm, Projection(preset="agentarts"))  # accumulate

    agent = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
        },
    )
    out = project_span(agent, Projection(preset="agentarts"))  # take + derive total
    attrs = out.attributes
    assert attrs["gen_ai.usage.input_tokens"] == 455
    assert attrs["gen_ai.usage.output_tokens"] == 831
    assert attrs["gen_ai.usage.total_tokens"] == 1286


def test_agentarts_agent_tokens_present_not_overridden(make_span):
    """AGENT root already carrying usage is not overridden (host values
    trusted); total is derived from the host values."""
    llm = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 20,
        },
    )
    project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.usage.output_tokens": 3,
        },
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.usage.input_tokens"] == 7
    assert out.attributes["gen_ai.usage.output_tokens"] == 3
    assert out.attributes["gen_ai.usage.total_tokens"] == 10


def test_agentarts_agent_tokens_cache_miss_leaves_unset(make_span):
    """No earlier LLM span (nothing to aggregate): usage keys are left unset."""
    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert "gen_ai.usage.input_tokens" not in out.attributes
    assert "gen_ai.usage.output_tokens" not in out.attributes
    assert "gen_ai.usage.total_tokens" not in out.attributes


def test_agentarts_llm_usage_missing_not_aggregated(make_span):
    """An LLM span without usage attributes produces no aggregation entry
    (AGENT never writes zero values)."""
    llm = make_span(
        "chat model",
        attributes={"gen_ai.operation.name": "chat"},
    )
    project_span(llm, Projection(preset="agentarts"))

    agent = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(agent, Projection(preset="agentarts"))
    assert "gen_ai.usage.input_tokens" not in out.attributes
    assert "gen_ai.usage.total_tokens" not in out.attributes


def test_agentarts_system_instructions_flattened(make_span):
    """System instructions in parts-array form are flattened to plain text
    (official probe contract)."""
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.system_instructions": json.dumps(
                [
                    {"type": "text", "content": "You are a rigorous assistant."},
                    {"type": "text", "content": " Execute steps strictly."},
                ],
                ensure_ascii=False,
            ),
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.system_instructions"] == "You are a rigorous assistant. Execute steps strictly."


def test_agentarts_system_instructions_plain_string_untouched(make_span):
    """System instructions already in plain text are untouched."""
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.system_instructions": "You are a personal assistant.",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.system_instructions"] == "You are a personal assistant."


def test_agentarts_system_instructions_malformed_untouched(make_span):
    """Malformed JSON arrays / arrays without content fields keep the
    original value."""
    for raw in ('[broken', '[{"type": "text"}]', '"not-a-list"'):
        span = make_span(
            "chat model",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.system_instructions": raw,
            },
        )
        out = project_span(span, Projection(preset="agentarts"))
        assert out.attributes["gen_ai.system_instructions"] == raw


# ---- AgentArts preset: display-name compression (§4.3.8) ----


def test_agentarts_agent_root_name_compressed(make_span):
    """AGENT root (agent.{mode}.{session}) -> 'invoke_agent openjiuwen'."""
    span = make_span(
        "agent.agent.work.normal.web_1a1e2f0e328_d36c63fa448b",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "gen_ai.operation.name": "invoke_agent",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.name == "invoke_agent openjiuwen"
    assert span.name == "agent.agent.work.normal.web_1a1e2f0e328_d36c63fa448b"  # original span untouched


def test_agentarts_invoke_name_compressed(make_span):
    """agent.{type}.invoke -> '{operation.name} {type}'."""
    span = make_span(
        "agent.main_agent.invoke",
        attributes={"gen_ai.operation.name": "invoke_agent"},
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.name == "invoke_agent main_agent"


def test_agentarts_drops_deepagent_attrs(make_span):
    """On task_iteration containers (referenced by child spans, 0.1.20
    shape) both deepagent.* and openjiuwen.* are trimmed as whole
    namespaces; gen_ai is kept."""
    task_id = 0xAAAA000000000001
    # project the referencing child span first (react rounds end before the
    # parent -- parent references are recorded)
    child = make_span(
        "agent.main_agent.react_iteration.1",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        parent_span_id=task_id,
    )
    assert project_span(child, Projection(preset="agentarts")) is not None
    span = make_span(
        "agent.main_agent.task_iteration.1",
        attributes={
            "openjiuwen.trajectory.record.kind": "step",
            "openjiuwen.run.id": "cron-abc:1794822600",
            "openjiuwen.turn.number": 1,
            "deepagent.task.is_follow_up": False,
            "deepagent.task.iteration": 1,
            "deepagent.task.loop_event": 'event_type=<EventType.INPUT: "input"> event_id="1a90"',
            "gen_ai.conversation.id": "sess-1",
        },
        span_id=task_id,
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    attrs = out.attributes
    assert "deepagent.task.is_follow_up" not in attrs
    assert "deepagent.task.iteration" not in attrs
    assert "deepagent.task.loop_event" not in attrs
    assert "openjiuwen.run.id" not in attrs
    assert "openjiuwen.turn.number" not in attrs
    assert attrs["gen_ai.conversation.id"] == "sess-1"
    assert out.name == "task 1"  # trimming doesn't affect display-name derivation (rewrite precedes trim)


def test_agentarts_task_dangling_marker_dropped(make_span):
    """dev-stable/0.1.16 shape: a task_iteration with no child span references
    (react rounds hang directly off the AGENT root) -> dangling marker; the
    whole span is not reported (projection returns None)."""
    # react rounds hang off another parent (e.g. the AGENT root) -- no
    # reference to the task span
    child = make_span(
        "agent.main_agent.react_iteration.1",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        parent_span_id=0xBBBB000000000001,
    )
    assert project_span(child, Projection(preset="agentarts")) is not None
    task = make_span(
        "agent.main_agent.task_iteration.1",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=0xCCCC000000000001,
    )
    assert project_span(task, Projection(preset="agentarts")) is None


def test_agentarts_task_container_kept_when_referenced(make_span):
    """develop/0.1.20 shape: a task_iteration referenced by react rounds
    (container) -> kept, display name 'task N' -- trimming it would orphan
    the child spans and break the platform tree."""
    task_id = 0xDDDD000000000001
    child = make_span(
        "agent.main_agent.react_iteration.2",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        parent_span_id=task_id,
    )
    assert project_span(child, Projection(preset="agentarts")) is not None
    task = make_span(
        "agent.main_agent.task_iteration.1",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=task_id,
    )
    out = project_span(task, Projection(preset="agentarts"))
    assert out is not None
    assert out.name == "task 1"


def test_agentarts_agent_output_from_langfuse_fallback(make_span):
    """openjiuwen 0.1.16 contract: run spans have no span.output; the final
    answer is extracted from the langfuse.observation.output JSON and wrapped
    as structured output.messages."""
    answer = "System time 2026-11-17 11:30, load 0.08, inspection normal."
    span = make_span(
        "agent.agent.work.normal.cron-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "gen_ai.operation.name": "invoke_agent",
            "langfuse.observation.output": json.dumps(
                {"output": answer, "result_type": "answer", "finish_reason": "stop"},
                ensure_ascii=False,
            ),
            "langfuse.observation.type": "agent",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert out.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}],
        ensure_ascii=False,
    )
    # the source key is subsequently trimmed by namespace
    assert "langfuse.observation.output" not in out.attributes


def test_agentarts_agent_output_langfuse_raw_newlines(make_span):
    """Shape A (observed 13:10): double quotes + raw newline control
    characters -- parsed tolerantly with strict=False."""
    answer = "This inspection could not complete: shell execution environment error.\n\n- Symptom: bash 409."
    span = make_span(
        "agent.agent.work.normal.cron-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "gen_ai.operation.name": "invoke_agent",
            # simulating the host's unescaped concatenation: the string
            # contains a real newline, not a \n escape sequence
            "langfuse.observation.output": (
                '{"output": "' + answer + '", "result_type": "answer"}'
            ),
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert out.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}],
        ensure_ascii=False,
    )


def test_agentarts_agent_output_langfuse_py_repr(make_span):
    """Shape B (captured live from run_now at 14:31): single-quoted Python
    dict repr + literal \\n escape sequences -- JSON must fail; ast.literal_eval
    is the fallback."""
    answer = "Inspection at 2026-11-17 14:31:50 (Asia/Shanghai).\n\n- Result: failed, no load data."
    # the host's str(dict) output: single quotes + repr escaping (real
    # newlines became literal \n sequences)
    raw_value = str({"output": answer, "result_type": "answer"})
    assert raw_value.startswith("{'output': '")  # guard: ensures shape B is simulated
    span = make_span(
        "agent.agent.work.normal.cron-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "gen_ai.operation.name": "invoke_agent",
            "langfuse.observation.output": raw_value,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert out.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}],
        ensure_ascii=False,
    )


def test_agentarts_agent_output_langfuse_malformed_leaves_missing(make_span):
    """Malformed langfuse wrapper (non-JSON / output not a string) ->
    treated as missing; never writes a dirty value."""
    span = make_span(
        "agent.agent.work.normal.cron-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.trajectory.record.kind": "turn",
            "gen_ai.operation.name": "invoke_agent",
            "langfuse.observation.output": "{not-json",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out is not None
    assert "gen_ai.output.messages" not in out.attributes


def test_agentarts_events_whitelist_keeps_only_skill_loaded(make_span):
    """Host streaming chunk events are not reported; only the skill.loaded
    detection event is kept."""
    span = make_span(
        "chat model",
        attributes={"gen_ai.operation.name": "chat"},
        events=[
            Event("openjiuwen.stream.chunk", {"delta": "hel"}),
            Event("llm.chunk", {"delta": "lo"}),
            Event("skill.loaded", {"skill.name": "jw-flow"}),
        ],
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert [event.name for event in out.events] == ["skill.loaded"]
    assert out.events[0].attributes["skill.name"] == "jw-flow"
    # original span's events untouched
    assert len(span.events) == 3


def test_agentarts_events_cleared_when_no_skill(make_span):
    """Without a skill.loaded event the exported copy has empty events."""
    span = make_span(
        "chat model",
        attributes={"gen_ai.operation.name": "chat"},
        events=[Event("openjiuwen.stream.chunk", {"delta": "x"})],
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert len(out.events) == 0
    assert len(span.events) == 1  # original span untouched


def test_agentarts_links_cleared(make_span):
    """The agentarts preset clears links (the platform builds the tree from
    parent_span_id and doesn't consume links)."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Link, SpanContext, SpanKind, TraceFlags

    linked = Link(
        SpanContext(
            trace_id=0x0102030405060708090A0B0C0D0E0F10,
            span_id=0x9988776655443322,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    span = ReadableSpan(
        name="chat model",
        context=SpanContext(
            trace_id=0x0102030405060708090A0B0C0D0E0F10,
            span_id=0x1122334455667788,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        parent=None,
        resource=None,
        attributes={"gen_ai.operation.name": "chat"},
        events=[],
        links=[linked],
        kind=SpanKind.INTERNAL,
        status=None,
        start_time=1,
        end_time=2,
        instrumentation_scope=None,
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert len(out.links) == 0
    assert len(span.links) == 1  # original span untouched


def test_agentarts_iteration_names_shortened(make_span):
    """react_iteration -> 'react step' (user request 2026-11-21: display
    names drop the round suffix; the round is carried only via the
    gen_ai.react.round attribute; task_iteration name compression is covered
    by the container-detection cases -- dangling shapes are trimmed whole,
    container shapes stay as 'task N')."""
    span1 = make_span(
        "agent.main_agent.react_iteration.1",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
    )
    out1 = project_span(span1, Projection(preset="agentarts"))
    assert out1.name == "react step"
    assert out1.attributes["gen_ai.react.round"] == 1
    span2 = make_span(
        "agent.main_agent.react_iteration.2",
        attributes={"openjiuwen.trajectory.record.kind": "step"},
        span_id=0x1122334455667793,
    )
    out2 = project_span(span2, Projection(preset="agentarts"))
    assert out2.name == "react step"
    # the round number is carried by gen_ai.react.round (display name is
    # non-distinguishing)
    assert out2.attributes["gen_ai.react.round"] == 2


def test_agentarts_reasoning_span_dropped(make_span):
    """llm.reasoning child spans are not reported at all
    (AGENTARTS_DROP_SPANS): thinking content is already carried in the parent
    LLM span's output.messages (role=reasoning); reporting it separately
    would only produce a fake LLM node with degraded model_name and zero
    tokens."""
    span = make_span(
        "llm.reasoning",
        attributes={
            "gen_ai.span.kind": "LLM",
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.reasoning.output_tokens": 1326,
        },
    )
    assert project_span(span, Projection(preset="agentarts")) is None


def test_agentarts_tool_name_style_unified(make_span):
    """TOOL display names are unified: 0.1.16 'tool.bash' ->
    'execute_tool bash' (0.1.20's native 'execute_tool bash' untouched);
    tool.* names on non-TOOL kinds untouched."""
    # 0.1.16 dot-style template
    span = make_span(
        "tool.bash",
        attributes={
            "gen_ai.tool.name": "bash",
            "openjiuwen.trajectory.record.kind": "tool",
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.name == "execute_tool bash"
    # 0.1.20 native space-style template untouched
    span2 = make_span(
        "execute_tool read_file",
        attributes={
            "gen_ai.tool.name": "read_file",
            "openjiuwen.trajectory.record.kind": "tool",
        },
    )
    out2 = project_span(span2, Projection(preset="agentarts"))
    assert out2.name == "execute_tool read_file"
    # non-TOOL kind (e.g. an LLM name that happens to be tool.*) untouched
    span3 = make_span(
        "tool.x",
        attributes={"gen_ai.operation.name": "chat"},
    )
    out3 = project_span(span3, Projection(preset="agentarts"))
    assert out3.name == "tool.x"


def test_agentarts_non_agent_name_untouched(make_span):
    """Span names without the agent.* prefix are untouched."""
    span = make_span("chat model", attributes={"gen_ai.operation.name": "chat"})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.name == "chat model"


def test_agentarts_agent_name_without_operation_falls_back(make_span):
    """Without operation.name the AGENT name falls back to invoke_agent."""
    span = make_span(
        "agent.code.sess-1",
        attributes={"openjiuwen.trace.root": True},
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.name == "invoke_agent openjiuwen"


def test_agentarts_total_tokens_dirty_values(make_span):
    span = make_span(
        "chat model",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": "N/A",
            "gen_ai.usage.output_tokens": 30,
        },
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.usage.total_tokens"] == 30


def test_agentarts_no_usage_no_total(make_span):
    span = make_span("chat model", attributes={"gen_ai.operation.name": "chat"})
    out = project_span(span, Projection(preset="agentarts"))
    assert "gen_ai.usage.total_tokens" not in out.attributes


# ---- Skill detection (design.md §4.3.5) ----


def test_skill_md_hit_derives_attrs_and_event(make_span):
    span = _tool_span(make_span, {"path": ".agents/skills/jw-coding-flow/SKILL.md"})
    out = project_span(span, Projection(preset="agentarts"))
    attrs = out.attributes
    assert attrs["gen_ai.skill.name"] == "jw-coding-flow"
    assert attrs["gen_ai.skill.id"] == "jw-coding-flow"
    assert attrs["gen_ai.span.kind"] == "TOOL"
    events = out.events
    assert len(events) == 1
    assert events[0].name == "skill.loaded"
    assert events[0].attributes["skill.name"] == "jw-coding-flow"
    assert events[0].attributes["skill.path"] == ".agents/skills/jw-coding-flow/SKILL.md"
    # the original span's event list is not mutated
    assert len(span.events) == 0


def test_skill_md_hit_windows_separator(make_span):
    span = _tool_span(make_span, {"path": "E:\\repo\\skills\\demo\\SKILL.md"})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "demo"


def test_skill_md_hit_nested_arguments(make_span):
    span = _tool_span(make_span, {"options": {"file_path": "skills/deep/SKILL.md"}})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "deep"
    assert len(out.events) == 1


def test_skill_dir_segment_hit_no_event(make_span):
    span = _tool_span(make_span, {"dir": "skills/code-review"})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "code-review"
    assert len(out.events) == 0


def test_skill_no_hit_plain_path(make_span):
    span = _tool_span(make_span, {"path": "src/main.py"})
    out = project_span(span, Projection(preset="agentarts"))
    assert "gen_ai.skill.name" not in out.attributes
    assert len(out.events) == 0


def test_skill_yields_to_existing_name(make_span):
    span = _tool_span(
        make_span,
        {"path": "skills/other/SKILL.md"},
        extra_attrs={"gen_ai.skill.name": "official"},
    )
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "official"
    assert out.attributes["gen_ai.skill.id"] == "official"  # only the missing id is filled
    assert len(out.events) == 0  # no duplicate event appended


def test_skill_yields_to_existing_loaded_event(make_span):
    span = _tool_span(
        make_span,
        {"path": "skills/a/SKILL.md"},
    )
    span = _replay_with_event(span, Event("skill.loaded", {"skill.name": "x"}, 1))
    out = project_span(span, Projection(preset="agentarts"))
    # no name attribute -> name can still be inferred; event already
    # present -> not appended again
    assert out.attributes["gen_ai.skill.name"] == "a"
    assert len(out.events) == 1
    assert out.events[0].attributes["skill.name"] == "x"


def test_skill_multiple_hits_first_wins(make_span):
    span = _tool_span(make_span, {"path": "skills/a/SKILL.md", "dir": "skills/b"})
    out = project_span(span, Projection(preset="agentarts"))
    assert out.attributes["gen_ai.skill.name"] == "a"


def test_skill_custom_patterns(make_span):
    span = _tool_span(make_span, {"path": "specs/demo/README.md"})
    out = project_span(
        span,
        Projection(preset="agentarts", skill_path_patterns=["README.md"]),
    )
    assert out.attributes["gen_ai.skill.name"] == "demo"
    assert len(out.events) == 1


def test_skill_disabled_without_preset(make_span):
    span = _tool_span(make_span, {"path": "skills/x/SKILL.md"})
    out = project_span(
        span,
        Projection(skill_path_patterns=["SKILL.md"]),  # preset defaults to none
    )
    assert "gen_ai.skill.name" not in out.attributes


def _replay_with_event(span, event):
    """Rebuild the span from its attributes plus one pre-existing event
    (simulating an already-present skill.loaded)."""
    from agentarts_instrumentation.exporter.transform import clone_readable_span

    return clone_readable_span(span, span.attributes, list(span.events) + [event])
