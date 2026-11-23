# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L5 real-LLM live: remote ECS runs a real deepseek production sequence and
reports -> the local machine asserts via platform echo.

Cross-machine orchestration: the local machine (SAC not restarted) cannot
import openai (the DeepSeek provider dependency), while the ECS (Linux
environment + production-sequence script) has no KooCLI credentials -- so
the case orchestrates "plink remote reporting + local KooCLI assertion"
inside pytest, each side doing what it's good at.

Only the "network location" is mocked: the LLM call, event flow, spans,
gRPC reporting, and platform ingestion are all real.

Environment variables (skip when missing): AGENTARTS_TRACE_TOKEN /
AGENTARTS_AGENT_ID (group-level), DEEPSEEK_API_KEY, ECS_PASSWORD (never
committed).
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
_ECS_PASSWORD = os.environ.get("ECS_PASSWORD", "")
_ECS_HOSTKEY = os.environ.get("ECS_HOSTKEY", "")
_ECS_TARGET = os.environ.get("ECS_TARGET", "")

pytestmark = pytest.mark.skipif(
    not _DEEPSEEK_KEY
    or not _ECS_PASSWORD
    or not _ECS_HOSTKEY
    or not _ECS_TARGET,
    reason="DEEPSEEK_API_KEY / ECS_PASSWORD / ECS_HOSTKEY / ECS_TARGET not set; skipping L5 real-LLM live",
)

_PLINK = r"C:\Program Files\PuTTY\plink.exe"


def _run_remote(token: str, timeout: int = 300) -> str:
    """Run the real-LLM production-sequence script on the ECS via plink and
    return the combined output."""
    remote = (
        f"export AGENTARTS_TRACE_TOKEN='{token}' "
        f"DEEPSEEK_API_KEY='{_DEEPSEEK_KEY}'; "
        "/root/venv/bin/python /root/oj_real_llm_ecs.py 2>&1 | "
        "grep -E 'SESSION_ID|REAL ANSWER|USAGE|force_flush'"
    )
    result = subprocess.run(
        [
            _PLINK,
            "-ssh",
            "-batch",
            "-hostkey",
            _ECS_HOSTKEY,
            "-pw",
            _ECS_PASSWORD,
            _ECS_TARGET,
            remote,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout + result.stderr


def test_l5_real_llm_live(agentarts_env, platform_query):
    """L5: ECS real deepseek production sequence -> platform echo fully
    asserted: tree/aggregation/rename/real tokens."""
    output = _run_remote(agentarts_env["token"])

    session_match = re.search(r"SESSION_ID: (real-llm-\S+)", output)
    answer_match = re.search(r"REAL ANSWER: (.+)", output)
    usage_match = re.search(
        r"input_tokens=(\d+)\s+output_tokens=(\d+)", output
    )
    assert session_match, output[-500:]
    assert answer_match, output[-500:]
    assert usage_match, output[-500:]
    assert "force_flush: True" in output, output[-500:]

    session_id = session_match.group(1)
    answer_text = answer_match.group(1).strip()
    input_tokens = int(usage_match.group(1))
    output_tokens = int(usage_match.group(2))
    assert input_tokens > 0 and output_tokens > 0, "real usage should be positive"

    # platform echo (minute-level ingestion latency -> poll)
    spans = platform_query["wait_for_echo_tree"](session_id, min_spans=3)
    kinds = {span.get("span_type") for span in spans}
    assert {"AGENT", "LLM"}.issubset(kinds), [s.get("span_type") for s in spans]

    agent = next(s for s in spans if s.get("span_type") == "AGENT")
    llm = next(s for s in spans if s.get("span_type") == "LLM")
    assert llm["trace_id"] == agent["trace_id"]

    # AGENT input: cross-span aggregation (the real deepseek input's first
    # message is user)
    assert agent["input"], "AGENT input should be non-empty (LLM span aggregation)"
    # AGENT output: rename, consistent with the real answer
    assert agent["output"].strip() == answer_text, "platform output should match the real answer"
    # real token counts: platform echo == deepseek API response
    assert agent["input_tokens"] == input_tokens
    assert agent["tokens"] == input_tokens + output_tokens
    # the real answer is visible on the LLM span
    assert answer_text[:30] in llm["output"]
