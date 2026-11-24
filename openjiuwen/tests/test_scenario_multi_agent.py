# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Multi-agent (team) scenario test: the full host span tree through the
agentarts projection, mirroring the shape documented by
``openjiuwen.agent_teams.observability.rail`` (jiuwenswarm / agent-core)::

    team.{name}
    ├── agent.{member}.task_iteration.N   [AGENT]
    │     ├── llm.call                    [GENERATION]
    │     └── tool.{name}                 [TOOL]
    └── task.{id}                         [SPAN]

Projection order follows export reality: leaves are projected before their
containers (the parent-reference registry records child->parent links at
projection time, which is what keeps member task containers alive), and the
team root last.
"""

from __future__ import annotations

import json

import pytest
from agentarts_instrumentation.config import Projection
from agentarts_instrumentation.openjiuwen.preset import (
    _error_aggregator,
    _input_aggregator,
    _parent_refs,
    _round_counter,
    _token_aggregator,
)
from agentarts_instrumentation.projection import project_span


@pytest.fixture(autouse=True)
def _reset_aggregators():
    """Module-level singletons; make_span uses a fixed trace_id -- clear per
    test to prevent cross-test leakage."""
    for registry in (
        _input_aggregator,
        _token_aggregator,
        _error_aggregator,
        _parent_refs,
        _round_counter,
    ):
        registry.clear()
    yield
    for registry in (
        _input_aggregator,
        _token_aggregator,
        _error_aggregator,
        _parent_refs,
        _round_counter,
    ):
        registry.clear()


LEADER_ANSWER = "Team report: main agent finished the search, helper summarized."


def _project_team_trace(make_span) -> dict[str, object]:
    """Build and project the multi-agent tree; returns projected outputs."""
    root_id = 0xAAA000000000001
    main_task_id = 0xAAA000000000002
    main_llm_id = 0xAAA000000000003
    main_tool_id = 0xAAA000000000004
    helper_task_id = 0xAAA000000000005
    helper_llm_id = 0xAAA000000000006
    monitor_id = 0xAAA000000000007
    projection = Projection(preset="agentarts")
    out: dict[str, object] = {}

    # -- member leaves first (export ends leaves before containers) --
    out["main_llm"] = project_span(
        make_span(
            "llm.call",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 80,
                "openjiuwen.trajectory.record.kind": "inference",
            },
            span_id=main_llm_id,
            parent_span_id=main_task_id,
        ),
        projection,
    )
    out["main_tool"] = project_span(
        make_span(
            "tool.search",
            attributes={
                "gen_ai.tool.name": "search",
                "gen_ai.tool.call.arguments": json.dumps({"query": "system load"}),
            },
            span_id=main_tool_id,
            parent_span_id=main_task_id,
        ),
        projection,
    )
    out["helper_llm"] = project_span(
        make_span(
            "llm.call",
            attributes={
                "gen_ai.operation.name": "chat",
                "openjiuwen.trajectory.record.kind": "inference",
            },
            span_id=helper_llm_id,
            parent_span_id=helper_task_id,
        ),
        projection,
    )

    # -- member containers: referenced by their children -> kept --
    out["main_task"] = project_span(
        make_span(
            "agent.main.task_iteration.1",
            attributes={
                "openjiuwen.trajectory.record.kind": "step",
                "gen_ai.agent.name": "main",
                "deepagent.task.iteration": 1,
            },
            span_id=main_task_id,
            parent_span_id=root_id,
        ),
        projection,
    )
    out["helper_task"] = project_span(
        make_span(
            "agent.helper.task_iteration.1",
            attributes={
                "openjiuwen.trajectory.record.kind": "step",
                "gen_ai.agent.name": "helper",
                "deepagent.task.iteration": 1,
            },
            span_id=helper_task_id,
            parent_span_id=root_id,
        ),
        projection,
    )

    # -- team monitor span: WORKFLOW by name prefix --
    out["monitor"] = project_span(
        make_span("task.t-101", span_id=monitor_id, parent_span_id=root_id),
        projection,
    )

    # -- team root last: leader result stamped by the team rail (0.1.16
    #    contract: langfuse.observation.output JSON on the trace root) --
    out["root"] = project_span(
        make_span(
            "team.alpha",
            attributes={
                "openjiuwen.trace.root": True,
                "openjiuwen.trajectory.record.kind": "turn",
                "gen_ai.conversation.id": "team-sess-1",
                "agentteam.session.id": "team-1",
                "agentteam.agent.name": "alpha",
                "langfuse.observation.output": json.dumps(
                    {"output": LEADER_ANSWER, "result_type": "answer"},
                    ensure_ascii=False,
                ),
            },
            span_id=root_id,
        ),
        projection,
    )
    return out


def test_team_scenario_full_tree_survives(make_span):
    """Every node survives: member task containers are referenced by their
    children, so the dangling-marker trim never fires in team scenarios."""
    out = _project_team_trace(make_span)
    for key, span in out.items():
        assert span is not None, key


def test_team_scenario_root_projection(make_span):
    """Team trace root: trace-root kind priority (AGENT), leader answer
    extracted to gen_ai.output.messages, team-identity duplicates trimmed."""
    root = _project_team_trace(make_span)["root"]
    assert root.name == "team.alpha"  # non-agent./tool. names pass through
    assert root.attributes["gen_ai.span.kind"] == "AGENT"
    assert root.attributes["gen_ai.output.messages"] == json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": LEADER_ANSWER}]}],
        ensure_ascii=False,
    )
    assert root.attributes["gen_ai.conversation.id"] == "team-sess-1"
    assert "agentteam.session.id" not in root.attributes
    assert "agentteam.agent.name" not in root.attributes
    assert "langfuse.observation.output" not in root.attributes


def test_team_scenario_member_containers(make_span):
    """Member containers kept with display name 'task N'; the member name
    survives via gen_ai.agent.name while deepagent.* is trimmed."""
    out = _project_team_trace(make_span)
    for key in ("main_task", "helper_task"):
        assert out[key].name == "task 1", key
        assert out[key].attributes["gen_ai.agent.name"] in ("main", "helper"), key
        assert "deepagent.task.iteration" not in out[key].attributes, key


def test_team_scenario_leaves(make_span):
    """Member leaves: LLM kind for generations, TOOL kind + unified
    execute_tool display name for tools; team monitor is WORKFLOW."""
    out = _project_team_trace(make_span)
    assert out["main_llm"].attributes["gen_ai.span.kind"] == "LLM"
    assert out["helper_llm"].attributes["gen_ai.span.kind"] == "LLM"
    assert out["main_tool"].attributes["gen_ai.span.kind"] == "TOOL"
    assert out["main_tool"].name == "execute_tool search"
    assert out["monitor"].attributes["gen_ai.span.kind"] == "WORKFLOW"
