# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentArts official access credential resolution.

Official access variables (issued by the console access guide; aligned
2026-11-20). Values can come from the process environment or from an
env file referenced via ``AGENTARTS_ENV_FILE``; the process environment
wins. Framework packs consume these to arm their runtime injection.
"""

from __future__ import annotations

import os

#: AgentArts official access variables (issued by the console access
#: guide; aligned 2026-11-20). Can be carried by process environment
#: or by an env file referenced via ``AGENTARTS_ENV_FILE``; process
#: environment wins.
ENV_TOKEN = "agentarts_agentops_apm_token"
ENV_ENDPOINT = "agentarts_agentops_apm_exporter_endpoint"
ENV_RESOURCE_ID = "agent_resource_id"
ENV_RESOURCE_TYPE = "resource_type"
ENV_DOMAIN_ID = "agent_domain_id"
ENV_PROJECT_ID = "agentarts_project_id"
ENV_APM_ENABLE = "agentarts_agentops_otel_agent_apm_enable"
#: 0.1.x early variable names (compat fallback; deprecated per README —
#: official access uses the group above).
ENV_TOKEN_LEGACY = "AGENTARTS_TRACE_TOKEN"
ENV_RESOURCE_ID_LEGACY = "AGENTARTS_AGENT_ID"
ENV_ENDPOINT_LEGACY = "AGENTARTS_TRACE_ENDPOINT"
#: Display-only variables (probe-owned, no official counterpart).
ENV_AGENT_NAME = "AGENTARTS_AGENT_NAME"
ENV_USER_ID = "AGENTARTS_USER_ID"
ENV_FILE = "AGENTARTS_ENV_FILE"

#: Default AgentArts access gateway (cn-southwest-2); AgentArts only
#: exposes a gRPC channel there (measured 2026-11).
DEFAULT_ENDPOINT = "https://apm-access.cn-southwest-2.myhuaweicloud.com"

#: Variable names considered when resolving settings.
_SETTINGS_KEYS = (
    ENV_TOKEN,
    ENV_TOKEN_LEGACY,
    ENV_ENDPOINT,
    ENV_ENDPOINT_LEGACY,
    ENV_RESOURCE_ID,
    ENV_RESOURCE_ID_LEGACY,
    ENV_RESOURCE_TYPE,
    ENV_DOMAIN_ID,
    ENV_PROJECT_ID,
    ENV_APM_ENABLE,
    ENV_AGENT_NAME,
    ENV_USER_ID,
)


def _read_env_file(path: str) -> dict[str, str]:
    """Minimal .env parser: ``KEY=VALUE`` lines, skip comments/blanks;
    first occurrence wins for duplicate keys.

    No full dotenv semantics (quotes/interpolation/multiline) — access
    config is all single-line KV. ``utf-8-sig`` tolerates BOM written by
    Windows Notepad.
    """
    settings: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                settings.setdefault(key.strip(), value.strip())
    except OSError:
        pass
    return settings


def _load_settings() -> dict[str, str]:
    """Resolve access config: process environment first, then the
    ``AGENTARTS_ENV_FILE`` file.

    Returns **resolved key-values** (not the full source map): each key
    keeps the first source that hit (env var > env file), values are
    stripped. Keys that hit nothing are absent from the result.
    """
    env_file = os.environ.get(ENV_FILE, "").strip()
    file_settings = _read_env_file(env_file) if env_file else {}
    settings: dict[str, str] = {}
    for key in _SETTINGS_KEYS:
        for source in (os.environ, file_settings):
            value = (source.get(key) or "").strip()
            if value:
                settings[key] = value
                break
    return settings
