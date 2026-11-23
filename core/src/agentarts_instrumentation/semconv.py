# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentArts platform semantic keys (gen_ai.* attribute contract).

Follows the Huawei Cloud doc "OpenTelemetry Protocol Field Mapping"
(version 2026-11-02,
https://support.huaweicloud.com/ops-agentarts/agentarts_14_0127.html).
Framework derivations (e.g. the jiuwen ``agentarts`` preset) map their
host data contract onto these keys; the core pipeline itself is key-
agnostic.
"""

from __future__ import annotations

# ---- Span typing / display ----
K_SPAN_KIND = "gen_ai.span.kind"
K_ERROR_RECOVERED = "gen_ai.error.recovered"
K_OPERATION_NAME = "gen_ai.operation.name"

# ---- Tool ----
K_TOOL_NAME = "gen_ai.tool.name"
K_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"

# ---- Agent ----
K_AGENT_NAME = "gen_ai.agent.name"

# ---- Token usage ----
K_USAGE_INPUT = "gen_ai.usage.input_tokens"
K_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
K_USAGE_TOTAL = "gen_ai.usage.total_tokens"
#: Cache token keys (LLM spans carry them directly; derivations may
#: aggregate them onto the AGENT root).
K_USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
K_USAGE_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"

# ---- Reasoning / skills ----
K_REACT_ROUND = "gen_ai.react.round"
K_SKILL_NAME = "gen_ai.skill.name"
K_SKILL_ID = "gen_ai.skill.id"

# ---- Prompts / messages ----
K_INPUT_MESSAGES = "gen_ai.input.messages"
K_OUTPUT_MESSAGES = "gen_ai.output.messages"
K_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"

# ---- Keys where host naming and the platform naming differ (renamed by
# derivations; see the jiuwen preset's AGENTARTS_RENAME) ----
K_CACHE_WRITE = "gen_ai.usage.cache_write.input_tokens"
K_REQUEST_STREAM = "gen_ai.request.stream"
K_TTFT = "gen_ai.response.time_to_first_token"

# ---- Resource / common attributes (platform field mapping table 10:
# resource.id/type required; domain.id for tenant isolation and
# gen_ai.project.id for project ownership) ----
K_RESOURCE_ID = "gen_ai.resource.id"
K_RESOURCE_TYPE = "gen_ai.resource.type"
K_USER_ID = "gen_ai.user.id"
K_PROJECT_ID = "gen_ai.project.id"
K_DOMAIN_ID = "domain.id"

__all__ = [
    "K_AGENT_NAME",
    "K_CACHE_WRITE",
    "K_DOMAIN_ID",
    "K_ERROR_RECOVERED",
    "K_INPUT_MESSAGES",
    "K_OPERATION_NAME",
    "K_OUTPUT_MESSAGES",
    "K_PROJECT_ID",
    "K_REACT_ROUND",
    "K_REQUEST_STREAM",
    "K_RESOURCE_ID",
    "K_RESOURCE_TYPE",
    "K_SKILL_ID",
    "K_SKILL_NAME",
    "K_SPAN_KIND",
    "K_SYSTEM_INSTRUCTIONS",
    "K_TOOL_CALL_ARGUMENTS",
    "K_TOOL_NAME",
    "K_TTFT",
    "K_USAGE_CACHE_CREATION",
    "K_USAGE_CACHE_READ",
    "K_USAGE_INPUT",
    "K_USAGE_OUTPUT",
    "K_USAGE_TOTAL",
    "K_USER_ID",
]
