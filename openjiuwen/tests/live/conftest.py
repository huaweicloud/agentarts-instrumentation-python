# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Environment bootstrap and shared facilities for the real AgentArts
reporting E2E group.

Unlike the other tests/ groups, this group **hits the real platform**
(gRPC reporting + KooCLI query echo). Credentials are read exclusively from
environment variables; the whole group skips when they are missing, so the
regular CI/local suite is unaffected.

Environment variables:
- AGENTARTS_TRACE_TOKEN / AGENTARTS_AGENT_ID -- required (group-level skip
  criteria)
- AGENTARTS_ENDPOINT -- optional (defaults to the cn-southwest-2 access
  address)
- DEEPSEEK_API_KEY -- only needed by the L5 real-LLM case
- HCLOUD_PATH -- optional, KooCLI executable location (two known locations
  probed by default)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

_TOKEN = os.environ.get("AGENTARTS_TRACE_TOKEN", "")
_AGENT_ID = os.environ.get("AGENTARTS_AGENT_ID", "")

if not _TOKEN or not _AGENT_ID:
    pytest.skip("AGENTARTS_TRACE_TOKEN / AGENTARTS_AGENT_ID not set; skipping real-reporting E2E", allow_module_level=True)

_ENDPOINT = os.environ.get(
    "AGENTARTS_ENDPOINT",
    "https://apm-access.cn-southwest-2.myhuaweicloud.com",
)

_HCLOUD_CANDIDATES = [
    os.environ.get("HCLOUD_PATH", ""),
    str(Path.home() / "AppData/Local/Temp/hcloudcli/hcloud.exe"),
    str(Path.home() / ".workbuddy/bin/hcloud/hcloud.exe"),
]


def find_hcloud() -> str:
    for candidate in _HCLOUD_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip(f"hcloud.exe not found (candidates: {_HCLOUD_CANDIDATES})", allow_module_level=True)


@pytest.fixture(scope="session")
def agentarts_env() -> dict:
    return {
        "token": _TOKEN,
        "agent_id": _AGENT_ID,
        "endpoint": _ENDPOINT,
    }


@pytest.fixture(scope="session")
def live_exporter(agentarts_env: dict):
    """A full AgentArts-shaped SDK exporter (real endpoint + official gRPC
    inner)."""
    from agentarts_instrumentation import ExporterConfig, Projection, build_exporter

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
    yield exporter
    exporter.shutdown()


@pytest.fixture(scope="session")
def platform_query():
    """Platform query facilities (KooCLI wrappers): echo polling + trace
    detail."""
    return {
        "query_traces": query_traces,
        "wait_for_echo": wait_for_echo,
        "wait_for_echo_tree": wait_for_echo_tree,
        "query_trace_detail": query_trace_detail,
    }


def query_traces(session_id: str | None = None, hours: float = 2.0) -> list:
    """KooCLI ListOpsTrace; with session_id None, fetches everything (by
    resource_id + time window)."""
    hcloud = find_hcloud()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(hours * 3600 * 1000)
    args = [
        hcloud,
        "AgentArts",
        "ListOpsTrace",
        "--cli-region=cn-southwest-2",
        f"--start_time={start_ms}",
        f"--end_time={end_ms}",
        "--page_no=1",
        "--page_size=50",
        f"--resource_id={_AGENT_ID}",
        "--cli-read-timeout=60",
    ]
    if session_id:
        args.append(f"--session_id={session_id}")
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    payload = json.loads(result.stdout)
    return payload.get("span_list", [])


def wait_for_echo(session_id: str, min_spans: int, timeout: float = 180.0) -> list:
    """Poll until the platform has ingested and echoed the data (the platform
    has minute-level indexing latency)."""
    deadline = time.monotonic() + timeout
    spans: list = []
    while time.monotonic() < deadline:
        spans = query_traces(session_id)
        if len(spans) >= min_spans:
            return spans
        time.sleep(10)
    return spans


def wait_for_echo_tree(session_id: str, min_spans: int, timeout: float = 180.0) -> list:
    """Fetch the full span tree in two steps.

    Child spans have an empty session_id on the platform (only the root
    carries conversation.id), so a session filter only finds the root --
    first locate the root by session to get trace_id, then pull the whole
    tree of that trace.
    """
    deadline = time.monotonic() + timeout
    roots: list = []
    while time.monotonic() < deadline:
        roots = [s for s in query_traces(session_id) if s.get("span_type") == "AGENT"]
        if roots:
            break
        time.sleep(10)
    if not roots:
        return []
    trace_id = roots[0]["trace_id"]
    tree: list = []
    while time.monotonic() < deadline:
        tree = [s for s in query_traces(hours=2.0) if s.get("trace_id") == trace_id]
        if len(tree) >= min_spans:
            return tree
        time.sleep(10)
    return tree


def query_trace_detail(trace_id: str) -> dict:
    """KooCLI ShowOpsTrace -- metadata contains the full spanAttributes
    (skill and other fields live there)."""
    hcloud = find_hcloud()
    result = subprocess.run(
        [
            hcloud,
            "AgentArts",
            "ShowOpsTrace",
            "--cli-region=cn-southwest-2",
            f"--trace_id={trace_id}",
            "--cli-read-timeout=60",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(result.stdout)
