# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests with the agent-core host stack (design.md §8).

Covers both span_exporter_override injection orderings, shutdown
propagation, and the agentarts preset acting on spans really produced by the
shared provider.
"""

from __future__ import annotations

import json

_FLUSH_TIMEOUT_MS = 5000


def _emit_chat_span(observability, attributes: dict | None = None) -> None:
    tracer = observability.get_tracer("huawei-otel-exporter-integration")
    with tracer.start_as_current_span("chat integration-model", attributes=attributes or {}):
        pass


# ---- ordering 1: the SDK is the first initializer (override replaces the
# factory product, single path) ----


def test_override_as_primary_exporter(observability, oj_config_factory, sdk_exporter):
    observability.init_observability(oj_config_factory(), span_exporter_override=sdk_exporter)
    _emit_chat_span(
        observability,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 20,
        },
    )
    assert sdk_exporter.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS) is True
    exported = sdk_exporter.collecting.exported
    assert len(exported) == 1
    attrs = exported[0].attributes
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.usage.total_tokens"] == 120


# ---- ordering 2: provider already exists (override appends a
# SimpleSpanProcessor, dual path) ----


def test_override_appended_to_existing_provider(observability, oj_config_factory, sdk_exporter):
    # first initializer: console primary exporter (provider established)
    observability.init_observability(oj_config_factory("console"))
    # second caller with override: not discarded; appends SimpleSpanProcessor
    observability.init_observability(oj_config_factory(), span_exporter_override=sdk_exporter)

    _emit_chat_span(observability, attributes={"gen_ai.operation.name": "chat"})

    assert sdk_exporter.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS) is True
    exported = sdk_exporter.collecting.exported
    assert len(exported) == 1
    assert exported[0].attributes["gen_ai.span.kind"] == "LLM"


# ---- shutdown propagation: when the provider closes, the SDK closes too,
# idempotently ----


def test_shutdown_propagates_to_sdk(observability, oj_config_factory, sdk_exporter):
    observability.init_observability(oj_config_factory(), span_exporter_override=sdk_exporter)
    assert observability.is_initialized() is True

    observability.shutdown_observability()
    assert observability.is_initialized() is False
    # propagation chain: provider.shutdown -> SimpleSpanProcessor.shutdown ->
    # TransformingSpanExporter.shutdown -> BufferedOtlpExporter.shutdown (idempotent)
    assert sdk_exporter.collecting.shutdown_count == 1


# ---- multi-level spans produced by the real provider are all projected correctly ----


def test_projection_over_real_provider_spans(observability, oj_config_factory, sdk_exporter):
    observability.init_observability(oj_config_factory(), span_exporter_override=sdk_exporter)
    tracer = observability.get_tracer("huawei-otel-exporter-integration")

    with tracer.start_as_current_span(
        "agent.code.sess-1",
        attributes={
            "openjiuwen.trace.root": True,
            "openjiuwen.agent.mode": "code",
            "gen_ai.conversation.id": "sess-1",
            "gen_ai.usage.input_tokens": 1280,
            "gen_ai.usage.output_tokens": 356,
        },
    ):
        with tracer.start_as_current_span(
            "chat integration-model",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
                "gen_ai.request.stream": True,
            },
        ):
            pass
        with tracer.start_as_current_span(
            "execute_tool read_file",
            attributes={
                "gen_ai.tool.name": "read_file",
                "gen_ai.tool.call.arguments": json.dumps(
                    {"path": ".agents/skills/jw-coding-flow/SKILL.md"}
                ),
            },
        ):
            pass

    assert sdk_exporter.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS) is True
    exported = sdk_exporter.collecting.exported
    assert len(exported) == 3

    by_kind = {span.attributes["gen_ai.span.kind"]: span for span in exported}
    assert set(by_kind) == {"AGENT", "LLM", "TOOL"}

    agent_attrs = by_kind["AGENT"].attributes
    assert agent_attrs["gen_ai.agent.name"] == "code"
    assert agent_attrs["gen_ai.usage.total_tokens"] == 1636

    llm_attrs = by_kind["LLM"].attributes
    assert llm_attrs["gen_ai.usage.total_tokens"] == 15
    assert llm_attrs["gen_ai.request.is_stream"] is True
    assert "gen_ai.request.stream" not in llm_attrs

    tool_attrs = by_kind["TOOL"].attributes
    assert tool_attrs["gen_ai.skill.name"] == "jw-coding-flow"
    loaded_events = [event for event in by_kind["TOOL"].events if event.name == "skill.loaded"]
    assert len(loaded_events) == 1
    assert loaded_events[0].attributes["skill.name"] == "jw-coding-flow"
