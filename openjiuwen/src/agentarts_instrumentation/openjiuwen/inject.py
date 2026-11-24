# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentArts runtime injection into openjiuwen hosts (in-package;
installed via the ``agentarts-instrumentation-inject`` command).

This module ships with the package, and the ``.pth`` written
by the installer references the in-package path — **upgrades are
zero-maintenance** (reinstalling the package repoints injection to the
new version, no file copying needed).

Deploy (inject right after pip install)::

    pip install "agentarts-instrumentation-openjiuwen[grpc]"  # 1. install (gRPC required)
    agentarts-instrumentation-inject            # 2. write .pth (idempotent)

From then on, **every** interpreter process of this venv executes
main() at startup: with credentials present, a deferred injection hook
is registered (armed); otherwise it skips — no process startup is ever
blocked. Credentials match the examples version (official access
variables ``agentarts_agentops_apm_token`` / ``agent_resource_id`` etc.
in the process environment, an env file referenced by
``AGENTARTS_ENV_FILE``, or — by default — the host's own
``~/.jiuwenswarm/config/.env``; aligned with the console access guide
2026-11-20; 0.1.x legacy names fall back).

Deferred injection (fix from 2026-11): the ``.pth`` runs during site
initialization, where importing openjiuwen immediately is **forbidden**
— measured, openjiuwen's import chain at that moment leaves
``fastmcp`` as a half-initialized module (``__getattr__`` not yet
defined), so the host's later ``from fastmcp import FastMCP`` raises
ImportError and jiuwenswarm workers fail to load (AgentServer runtime
failed). Instead we register a ``sys.meta_path`` finder: once the host
itself finishes importing
``openjiuwen(_teams).observability.setup``, patch that module's
``init_observability`` and sweep stale references bound earlier. The
host's import order is identical to before the probe was installed —
zero side effects.

Uninstall: run ``agentarts-instrumentation-uninject`` (removes the .pth) — or delete
``agentarts_instrumentation_openjiuwen_inject.pth`` under site-packages
manually — BEFORE ``pip uninstall`` of the package. The .pth content is
failure-guarded: once the package is gone it stays silent instead of
printing a traceback at every interpreter start. Existing host
processes keep their in-memory exporter until they restart.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentarts_instrumentation.internal.credentials import (
    DEFAULT_ENDPOINT,
    ENV_AGENT_NAME,
    ENV_APM_ENABLE,
    ENV_DOMAIN_ID,
    ENV_ENDPOINT,
    ENV_ENDPOINT_LEGACY,
    ENV_FILE,
    ENV_PROJECT_ID,
    ENV_RESOURCE_ID,
    ENV_RESOURCE_ID_LEGACY,
    ENV_RESOURCE_TYPE,
    ENV_TOKEN,
    ENV_TOKEN_LEGACY,
    ENV_USER_ID,
    _load_settings,
    _read_env_file,  # noqa: F401 - re-exported (tests / tooling import it from here)
)
from agentarts_instrumentation.internal.inject import (
    DeferredPatchFinder,
    build_pth_line,
)
from agentarts_instrumentation.internal.inject import (
    install_pth as _install_pth_generic,
)
from agentarts_instrumentation.internal.inject import (
    remove_pth as _remove_pth_generic,
)

_PTH_NAME = "agentarts_instrumentation_openjiuwen_inject.pth"
#: The .pth imports this module so its module-level main() arms the
#: injection in every interpreter process (the generic guarded one-liner
#: lives in the core toolkit).
_INJECT_MODULE = "agentarts_instrumentation.openjiuwen.inject"
_PTH_LINE = build_pth_line(_INJECT_MODULE)
_TAG = "[agentarts-instrumentation-inject]"

_EXT_SETUP_MODULE = "openjiuwen.extensions.observability.setup"
_TEAMS_SETUP_MODULE = "openjiuwen.agent_teams.observability.setup"
_TARGET_MODULES = (_EXT_SETUP_MODULE, _TEAMS_SETUP_MODULE)

#: Default AgentArts access gateway (cn-southwest-2; gRPC channel).
_DEFAULT_ENDPOINT = DEFAULT_ENDPOINT


def _default_env_file() -> str:
    """Default env file when ``AGENTARTS_ENV_FILE`` is unset: the host's
    own official workspace env (jiuwenswarm ``~/.jiuwenswarm/config/.env``)
    — the official access variables can simply be appended to the file
    the host already loads. Parsed identically to a user-specified file
    (matched surrounding quotes are stripped), and replaced entirely by
    ``AGENTARTS_ENV_FILE`` when that is set.
    """
    return str(Path.home() / ".jiuwenswarm" / "config" / ".env")


def _host_otlp_conflict(config: object) -> str | None:
    """Detect whether the host's native OTLP export already points at
    the AgentArts gateway (duplicating this probe).

    When the host ``ObservabilityConfig.exporter`` is
    ``otlp_grpc``/``otlp_http`` and ``endpoint`` targets a Huawei Cloud
    APM/AgentArts access gateway, the host already ships spans to the
    platform; injecting the probe on top would stack a second exporter
    on the same provider — the host runtime treats "override on an
    initialized provider" as **append** (runtime.py initialize()
    L139-140), so every span is delivered twice and the same span_id
    appears duplicated on the platform trace (reproduced on a
    colleague's environment 2026-11-20).

    Returns a conflict description (used in the standdown warning), or
    None when no conflict:

    - localhost / no endpoint configured: the native path targets local
      4317 (nothing listening) or has no exporter — the probe remains
      the sole platform data source, no conflict;
    - langfuse: a different platform, no conflict;
    - self-built collectors outside the AgentArts gateway: the probe
      cannot see their forwarding target, so no interception here
      (documented in README).
    """
    exporter = str(getattr(config, "exporter", "") or "")
    if exporter not in ("otlp_grpc", "otlp_http"):
        return None
    endpoint = str(getattr(config, "endpoint", "") or "").strip()
    if not endpoint:
        return None
    try:
        from urllib.parse import urlparse

        # gRPC endpoints may be bare host:port — add a scheme so the
        # hostname parses.
        host = urlparse(endpoint if "//" in endpoint else f"//{endpoint}").hostname or ""
    except ValueError:
        host = ""
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return None
    if "apm-access" in endpoint or "myhuaweicloud.com" in host:
        return f"exporter={exporter}, endpoint={endpoint}"
    return None


def main() -> bool:  # noqa: C901 - kept structurally identical to the examples version on purpose
    """Register the deferred injection hook; returns whether armed."""
    settings = _load_settings(default_env_file=_default_env_file())

    def pick(official: str, legacy: str = "") -> str:
        return settings.get(official, "") or settings.get(legacy, "")

    # APM trace export switch (official access variable): explicit
    # false/0/off disables; unset defaults to enabled — existing
    # deployments are unaffected. aom_enable is consumed by AOM-side
    # components; this probe only implements the APM trace channel and
    # never reads it.
    if pick(ENV_APM_ENABLE).lower() in ("false", "0", "no", "off"):
        print(f"{_TAG} skip: {ENV_APM_ENABLE} is disabled", file=sys.stderr)
        return False

    token = pick(ENV_TOKEN, ENV_TOKEN_LEGACY)
    agent_id = pick(ENV_RESOURCE_ID, ENV_RESOURCE_ID_LEGACY)
    endpoint = pick(ENV_ENDPOINT, ENV_ENDPOINT_LEGACY) or _DEFAULT_ENDPOINT
    if not token or not agent_id:
        print(
            f"{_TAG} skip: missing {ENV_TOKEN} / {ENV_RESOURCE_ID}",
            file=sys.stderr,
        )
        return False

    # Platform common attributes (official field mapping table 10):
    # resource.id/type required; domain.id for tenant isolation and
    # gen_ai.project.id for project ownership — written only when set.
    resource_type = pick(ENV_RESOURCE_TYPE) or "agent"
    domain_id = pick(ENV_DOMAIN_ID)
    project_id = pick(ENV_PROJECT_ID)
    agent_name = settings.get(ENV_AGENT_NAME, "") or agent_id
    user_id = settings.get(ENV_USER_ID, "") or "service-host-user"
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
    remap: list[tuple[object, object]] = []
    patched_modules: set[str] = set()
    finder_ref: list[object] = []

    def _build_exporter():
        nonlocal exporter
        if exporter is None:
            from agentarts_instrumentation import (
                ExporterConfig,
                Projection,
                build_exporter,
            )

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

    def _provider_has_exporter(provider, sdk_exporter) -> bool:
        """Check whether the provider already carries this SDK exporter
        (guards against double export)."""
        try:
            multi = getattr(provider, "_active_span_processor", None)
            for proc in getattr(multi, "_span_processors", ()) or ():
                if getattr(proc, "span_exporter", None) is sdk_exporter:
                    return True
        except Exception:  # noqa: BLE001 - detection failure must not block
            pass
        return False

    def _make_wrapper(orig_init):
        """Dual-mode wrapper: picks the injection style automatically
        based on how the host calls in."""
        standdown_logged: list[bool] = []

        def _init_with_agentarts(config, **kwargs):
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            # Host native OTLP already targets the AgentArts gateway:
            # stand down to avoid shipping the same span twice (see
            # _host_otlp_conflict docstring; warn once per process).
            conflict = _host_otlp_conflict(config)
            if conflict is not None:
                if not standdown_logged:
                    standdown_logged.append(True)
                    print(
                        f"{_TAG} host native OTLP already reports to AgentArts ({conflict}); "
                        f"probe stands down to avoid duplicate reporting. Pick one: keep "
                        f"the host's native export and uninstall the probe (delete "
                        f"{_PTH_NAME} under site-packages), "
                        f"or keep the probe (with platform semantic projection) and "
                        f"switch the host exporter to file/console.",
                        file=sys.stderr,
                    )
                return orig_init(config, **kwargs)

            provider = kwargs.get("tracer_provider_override")
            if (
                isinstance(provider, TracerProvider)
                and kwargs.get("span_exporter_override") is None
            ):
                # Host-built provider: after orig succeeds, append the
                # SDK processor (idempotent: dedupe by provider id, and
                # skip when the main exporter is already this SDK).
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

    def _sweep() -> int:
        """Repoint symbols already bound to the original function
        (bound earlier in the import chain than the patch)."""
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
        return swept

    def _on_module_ready(module) -> None:
        """Target module finished executing: patch init_observability
        and sweep stale bindings."""
        name = getattr(module, "__name__", "")
        if name in patched_modules:
            return
        orig_init = getattr(module, "init_observability", None)
        if orig_init is None:
            return
        try:
            wrapper = _make_wrapper(orig_init)
            module.init_observability = wrapper
            remap.append((orig_init, wrapper))
            patched_modules.add(name)
            swept = _sweep()
            print(
                f"{_TAG} AgentArts span exporter injection applied to {name} "
                f"(swept {swept} stale bindings)",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 - injection failure must not block startup
            print(f"{_TAG} patch failed on {name}: {exc}", file=sys.stderr)
            return
        if len(patched_modules) >= len(_TARGET_MODULES) and finder_ref:
            try:
                sys.meta_path.remove(finder_ref[0])
            except ValueError:
                pass

    # If a target module is already imported (theoretically not at the
    # .pth stage), patch immediately.
    for name in _TARGET_MODULES:
        module = sys.modules.get(name)
        if module is not None:
            _on_module_ready(module)
    if len(patched_modules) < len(_TARGET_MODULES):
        finder = DeferredPatchFinder(_TARGET_MODULES, _on_module_ready)
        sys.meta_path.insert(0, finder)
        finder_ref.append(finder)
    print(
        f"{_TAG} AgentArts span exporter injection armed "
        f"(deferred until openjiuwen import)",
        file=sys.stderr,
    )
    return True


def install_pth() -> int:
    """Entry point of the ``agentarts-instrumentation-inject`` command: write the .pth
    into this interpreter's site-packages (generic mechanics in the
    core toolkit).

    The .pth references the in-package module (guarded one-liner)
    instead of copying a file, so reinstalling/upgrading the package
    automatically repoints injection at the new version. Idempotent: an
    existing .pth with identical content is left alone; older content
    is upgraded in place.
    """
    code = _install_pth_generic(
        module=_INJECT_MODULE,
        pth_name=_PTH_NAME,
        package_marker="agentarts_instrumentation/openjiuwen",
    )
    if code == 0:
        print(
            f"{_TAG} Set the official access variables ({ENV_TOKEN} / "
            f"{ENV_RESOURCE_ID}) in the process environment, in the host's own "
            f"env file (default: {Path.home() / '.jiuwenswarm' / 'config' / '.env'}), "
            f"or point {ENV_FILE} at an env file carrying them; then restart the host "
            f"service to activate. Uninstall: "
            f"run agentarts-instrumentation-uninject. Processes without openjiuwen simply "
            f"skip (harmless)."
        )
    return code


def remove_pth() -> int:
    """Entry point of the ``agentarts-instrumentation-uninject`` command: delete the
    .pth from this interpreter's site-packages.

    Idempotent: a missing file is a success. Run this BEFORE
    ``pip uninstall agentarts-instrumentation-openjiuwen`` — the .pth line
    is failure-guarded (silent once the package is gone), but older
    unguarded builds would print a traceback at every interpreter start.
    Existing host processes keep their in-memory exporter until they
    restart.
    """
    code = _remove_pth_generic(pth_name=_PTH_NAME)
    if code == 0:
        print(
            f"{_TAG} injection disabled for new processes; existing host "
            f"processes keep exporting until they restart. Remove the SDK "
            f"packages with: pip uninstall agentarts-instrumentation-openjiuwen "
            f"agentarts-instrumentation-core"
        )
    return code


main()
