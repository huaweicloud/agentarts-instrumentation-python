# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentArts runtime injection template for service-style hosts
(.pth mechanism).

Use case: the host runs as a long-lived service / multiprocess tree
(e.g. jiuwenswarm app / agentserver / gateway / workers), so the SDK
exporter cannot be passed in via ``span_exporter_override`` — and the
host's observability config path (config.yaml ``otlp_grpc``) does not
satisfy the AgentArts contract anyway (jiuwenswarm goes through
agent-core ``build_span_exporter``: hardcoded ``insecure=True`` plus
Langfuse Basic auth, which cannot reach the AgentArts TLS gateway).

Deploy (inside the host service's venv):

1. Copy this file to
   ``<venv>/lib/<py>/site-packages/inject_service_host.py``;
2. Create ``inject_service_host.pth`` in the same directory with one
   line::

       import inject_service_host

3. Set environment variables (or a host .env file; official access
   variable names, identical to ``_inject.py``; aligned with the
   console access guide 2026-11-20)::

       agentarts_agentops_apm_token=<token>
       agentarts_agentops_apm_exporter_endpoint=https://apm-access.<region>.myhuaweicloud.com
       agent_resource_id=<agent_id>
       resource_type=agent                      # optional, default "agent"
       agent_domain_id=<domain_id>              # optional, injects domain.id
       agentarts_project_id=<project_id>        # optional, injects gen_ai.project.id
       agentarts_agentops_otel_agent_apm_enable=true   # optional; explicit false disables
       AGENTARTS_ENV_FILE=/path/to/host.env   # optional: all variables above may live in this file

Mechanics and pitfalls (all measured; see design.md §6.5):

- ``.pth`` files are processed by the site module at interpreter
  startup, before any business import — the host's
  ``from ... import init_observability`` binds to the patched version
  here; multiprocess trees are fully covered (every subprocess runs
  it).
- **sitecustomize.py does NOT work**: the Debian/Ubuntu system-level
  ``/usr/lib/<py>/sitecustomize.py`` (apport hook) precedes the venv's
  site-packages on sys.path, so a same-named file never loads.
- The injection module runs before the host's ``load_dotenv`` — the
  token must be read from the environment or the host .env file here;
  it cannot rely on host-loaded config.
- Injection failure (missing token / import error) only warns on
  stderr and never blocks service startup.
- **Dual entry patch** (openjiuwen versions consume
  init_observability through different paths; both must be patched):

  - ``openjiuwen.extensions.observability.setup`` — the extensions
    direct path (0.1.16 imports from the submodule; the 0.1.20
    top-level re-export also originates here);
  - ``openjiuwen.agent_teams.observability.setup`` — the teams path
    (consumed when the host does ``from
    openjiuwen.agent_teams.observability import init_observability``;
    in that version it's an independent definition that forwards
    internally to the shared extensions runtime).

- **Dual injection modes** (decided by how the host calls; the
  wrapper adapts automatically):

  - Host passes ``tracer_provider_override=<host-built provider>``
    (e.g. the jiuwenswarm dev-stable telemetry path): **original call
    semantics unchanged**; after orig init succeeds, append a
    ``SimpleSpanProcessor(SDK)`` to that provider (idempotent: dedupe
    by provider id, and skip when the provider already carries this
    SDK exporter — prevents double export when override injection and
    processor append coexist); compatible with the
    ``span_exporter_override`` mutual-exclusion check;
  - Host passes no provider (standalone agent-core / older
    jiuwenswarm): stuff ``span_exporter_override=SDK`` and let the
    shared runtime load it.

- **Binding sweep** (the critical cross-version pitfall, measured on
  openjiuwen 0.1.16): when this module itself does ``import
  agent_teams.observability.setup``, Python first executes the teams
  package ``__init__`` → ``rail.py`` → ``harness.observability``
  import chain — every ``init_observability`` /
  ``init_shared_observability`` symbol along that chain binds the
  **original function** before the patch takes effect. Afterwards,
  changing only the two setup submodule attributes cannot rescue those
  references (harness/teams package toplevels consume the original →
  the provider loads the host-configured exporter and the SDK is
  bypassed entirely). Hence, after patching, scan the attributes of
  all openjiuwen modules in ``sys.modules`` by **original-function
  object identity** and repoint stale bindings to the wrapper.
"""

from __future__ import annotations

import os
import sys

# Official access variables (same names as the in-package _inject.py;
# legacy 0.1.x names fall back).
ENV_TOKEN = "agentarts_agentops_apm_token"
ENV_TOKEN_LEGACY = "AGENTARTS_TRACE_TOKEN"
ENV_ENDPOINT = "agentarts_agentops_apm_exporter_endpoint"
ENV_ENDPOINT_LEGACY = "AGENTARTS_TRACE_ENDPOINT"
ENV_RESOURCE_ID = "agent_resource_id"
ENV_RESOURCE_ID_LEGACY = "AGENTARTS_AGENT_ID"
ENV_RESOURCE_TYPE = "resource_type"
ENV_DOMAIN_ID = "agent_domain_id"
ENV_PROJECT_ID = "agentarts_project_id"
ENV_APM_ENABLE = "agentarts_agentops_otel_agent_apm_enable"
ENV_FILE = "AGENTARTS_ENV_FILE"

_DEFAULT_ENDPOINT = "https://apm-access.cn-southwest-2.myhuaweicloud.com"


def _read_env_file(path: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, skip comments/blanks;
    first occurrence wins for duplicate keys."""
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


def _pick(key: str, legacy: str = "") -> str:
    """Process environment first, then the env file referenced by
    AGENTARTS_ENV_FILE."""
    env_file = os.environ.get(ENV_FILE, "").strip()
    sources = [os.environ]
    if env_file:
        sources.append(_read_env_file(env_file))
    for name in (key, legacy):
        if not name:
            continue
        for source in sources:
            value = (source.get(name) or "").strip()
            if value:
                return value
    return ""


def main() -> bool:
    """Load credentials and patch init_observability; returns whether
    injection succeeded."""
    if _pick(ENV_APM_ENABLE).lower() in ("false", "0", "no", "off"):
        print(f"[inject_service_host] skip: {ENV_APM_ENABLE} is disabled", file=sys.stderr)
        return False
    token = _pick(ENV_TOKEN, ENV_TOKEN_LEGACY)
    agent_id = _pick(ENV_RESOURCE_ID, ENV_RESOURCE_ID_LEGACY)
    endpoint = _pick(ENV_ENDPOINT, ENV_ENDPOINT_LEGACY) or _DEFAULT_ENDPOINT
    if not token or not agent_id:
        print(
            f"[inject_service_host] skip: missing {ENV_TOKEN} / {ENV_RESOURCE_ID}",
            file=sys.stderr,
        )
        return False
    try:
        import agentarts_instrumentation.jiuwen  # noqa: F401 - registers the agentarts derivation
        from agentarts_instrumentation import ExporterConfig, Projection, build_exporter
        from openjiuwen.extensions.observability import setup as oj_setup
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        try:
            from openjiuwen.agent_teams.observability import setup as teams_setup
        except Exception:  # noqa: BLE001 - the teams entry may not exist in some versions
            teams_setup = None
    except Exception as exc:  # noqa: BLE001 - injection failure must not block service startup
        print(f"[inject_service_host] skip: import failed: {exc}", file=sys.stderr)
        return False

    resource_type = _pick(ENV_RESOURCE_TYPE) or "agent"
    domain_id = _pick(ENV_DOMAIN_ID)
    project_id = _pick(ENV_PROJECT_ID)
    agent_name = os.environ.get("AGENTARTS_AGENT_NAME", "").strip() or agent_id
    user_id = os.environ.get("AGENTARTS_USER_ID", "service-host-user")
    common_attributes = {
        "gen_ai.resource.id": agent_id,
        "gen_ai.resource.type": resource_type,
        "gen_ai.agent.name": agent_name,
        "gen_ai.user.id": user_id,
    }
    if domain_id:
        common_attributes["domain.id"] = domain_id
    if project_id:
        common_attributes["gen_ai.project.id"] = project_id
    exporter = None
    attached_providers: set[int] = set()

    def _build_exporter():
        nonlocal exporter
        if exporter is None:
            exporter = build_exporter(
                ExporterConfig(
                    endpoint=endpoint,
                    # Measured (2026-11): the apm-access gateway only
                    # exposes a gRPC channel.
                    protocol="grpc",
                    # HTTP/2 metadata requires lowercase header names.
                    headers={"authentication": token},
                    projection=Projection(
                        preset="agentarts",
                        service_name=f"AgentArts.{agent_id}.default",
                        attributes=common_attributes,
                    ),
                )
            )
        return exporter

    def _provider_has_exporter(provider, exporter):
        """Check whether the provider already carries this SDK exporter
        (guards against double export)."""
        try:
            multi = getattr(provider, "_active_span_processor", None)
            for proc in getattr(multi, "_span_processors", ()) or ():
                if getattr(proc, "span_exporter", None) is exporter:
                    return True
        except Exception:  # noqa: BLE001 - detection failure must not block
            pass
        return False

    def _make_wrapper(orig_init):
        """Dual-mode wrapper: picks the injection style automatically
        based on how the host calls in."""

        def _init_with_agentarts(config, **kwargs):
            provider = kwargs.get("tracer_provider_override")
            if (
                isinstance(provider, TracerProvider)
                and kwargs.get("span_exporter_override") is None
            ):
                # Host-built provider: after orig succeeds, append the
                # SDK processor (idempotent) without changing the
                # original call semantics (span_exporter_override and
                # provider are mutually exclusive). Skip the append
                # when the provider's main exporter is already this SDK
                # (the override-injection path ran first) — otherwise
                # the same trace would be exported twice.
                result = orig_init(config, **kwargs)
                if id(provider) not in attached_providers:
                    attached_providers.add(id(provider))
                    sdk_exporter = _build_exporter()
                    if not _provider_has_exporter(provider, sdk_exporter):
                        provider.add_span_processor(SimpleSpanProcessor(sdk_exporter))
                return result
            if kwargs.get("span_exporter_override") is None:
                kwargs["span_exporter_override"] = _build_exporter()
            return orig_init(config, **kwargs)

        return _init_with_agentarts

    # Record the original function objects before patching (identity
    # basis for the binding sweep).
    orig_ext = oj_setup.init_observability
    wrapper_ext = _make_wrapper(orig_ext)
    oj_setup.init_observability = wrapper_ext
    remap: list[tuple[object, object]] = [(orig_ext, wrapper_ext)]
    if teams_setup is not None and teams_setup is not oj_setup:
        orig_teams = teams_setup.init_observability
        wrapper_teams = _make_wrapper(orig_teams)
        teams_setup.init_observability = wrapper_teams
        remap.append((orig_teams, wrapper_teams))

    # Binding sweep: the very import of teams setup above executed the
    # teams package ``__init__`` → ``rail`` → ``harness`` chain first,
    # whose symbols bound the original functions before the patch took
    # effect. Scan the attributes of all openjiuwen modules by object
    # identity and repoint stale bindings (including the
    # ``init_shared_observability`` alias) to the wrappers.
    swept = 0
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("openjiuwen"):
            continue
        for attr in tuple(vars(mod)):
            value = getattr(mod, attr, None)
            for orig, wrapped in remap:
                if value is orig:
                    try:
                        setattr(mod, attr, wrapped)
                        swept += 1
                    except Exception:  # noqa: BLE001 - single rebind failure is not fatal
                        pass
    print(
        "[inject_service_host] AgentArts span exporter injection armed "
        f"(swept {swept} stale bindings)",
        file=sys.stderr,
    )
    return True


main()
