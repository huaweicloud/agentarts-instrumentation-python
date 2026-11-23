# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic in-package runtime-injection toolkit (.pth installer +
deferred import-time patching).

Framework packs (e.g. agentarts-instrumentation-jiuwen) use this to hook
their SDK exporter into host runtimes: ``install_pth`` writes a guarded
.pth into the venv's site-packages that imports the framework pack's
inject module in **every** interpreter process; the pack's module-level
``main()`` then registers the injection hook.

Deferred injection rationale (measured on openjiuwen hosts, 2026-11):
running the patch during site initialization — importing the host
immediately at .pth time — leaves ``fastmcp`` as a half-initialized
module (``__getattr__`` not yet defined), so the host's later
``from fastmcp import FastMCP`` raises ImportError and workers fail to
load. Instead, register a ``sys.meta_path`` finder
(:class:`DeferredPatchFinder`): once the host itself finishes importing
the target module, the finder patches it right after execution and
sweeps stale references bound earlier — the host's import order stays
identical to before the pack was installed.
"""

from __future__ import annotations

import importlib.abc
import sys

_TAG = "[agentarts-inject]"


def build_pth_line(module: str) -> str:
    """Build the guarded one-line .pth content importing ``module``.

    The .pth line must start with "import " (site.addpackage contract).
    The exec-wrapped guard keeps the line a single statement while:
    - ModuleNotFoundError (package uninstalled, .pth left behind) → silent;
    - any other import failure → one-line warning, no full traceback.
    """
    return (
        "import sys; exec(\"try:\\n import "
        + module
        + "\\nexcept ModuleNotFoundError:\\n pass\\n"
        + "except Exception as e:\\n print('[agentarts-inject] injection load failed:', e, "
        'file=sys.stderr)")\n'
    )


def install_pth(
    module: str,
    pth_name: str,
    package_marker: str,
    tag: str = _TAG,
) -> int:
    """Write ``pth_name`` into this interpreter's site-packages.

    The .pth references the in-package inject module (guarded one-liner,
    see :func:`build_pth_line`) instead of copying a file, so
    reinstalling/upgrading the pack automatically repoints injection at
    the new version. ``package_marker`` is the site-packages-relative
    path that identifies the installed pack (e.g.
    ``agentarts_instrumentation/jiuwen``) — used to locate the right
    site directory.

    Idempotent: an existing .pth with identical content is left alone;
    older content is upgraded in place.
    """
    import site
    from pathlib import Path

    candidates = [Path(d) for d in site.getsitepackages()]
    target_dir = None
    for d in candidates:
        if (d / package_marker).is_dir() or (d / f"{package_marker}.py").is_file():
            target_dir = d
            break
    if target_dir is None:
        print(
            f"{tag} error: SDK package not found in this interpreter's "
            f"site-packages (candidates: {[str(c) for c in candidates]})",
            file=sys.stderr,
        )
        return 1

    content = build_pth_line(module)
    pth = target_dir / pth_name
    if pth.exists() and pth.read_text(encoding="utf-8").strip() == content.strip():
        print(f"{tag} already installed: {pth}")
        return 0
    upgraded = pth.exists()
    pth.write_text(content, encoding="utf-8")
    if upgraded:
        print(f"{tag} upgraded .pth content: {pth}")
    else:
        print(f"{tag} installed: {pth}")
    return 0


def remove_pth(pth_name: str, tag: str = _TAG) -> int:
    """Delete ``pth_name`` from this interpreter's site-packages.

    Idempotent: a missing file is a success. Run this BEFORE pip
    uninstalling the pack — the .pth line is failure-guarded (silent
    once the package is gone), but older unguarded builds would print a
    traceback at every interpreter start. Existing host processes keep
    their in-memory exporter until they restart.
    """
    import site
    from pathlib import Path

    removed = False
    for d in site.getsitepackages():
        pth = Path(d) / pth_name
        if pth.exists():
            pth.unlink()
            print(f"{tag} removed: {pth}")
            removed = True
    if not removed:
        print(f"{tag} no .pth found under site-packages (nothing to remove)")
    return 0


class _LoaderProxy:
    """Wraps a module loader and invokes the callback after the module
    finishes executing."""

    def __init__(self, loader, callback):
        self._loader = loader
        self._callback = callback

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        self._callback(module)


class DeferredPatchFinder(importlib.abc.MetaPathFinder):
    """Intercept the import of target modules and patch right after
    their execution completes."""

    def __init__(self, targets, callback):
        self._targets = set(targets)
        self._callback = callback
        self._in_flight: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._targets or fullname in self._in_flight:
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        self._in_flight.add(fullname)
        spec.loader = _LoaderProxy(spec.loader, self._callback)
        return spec


__all__ = [
    "DeferredPatchFinder",
    "build_pth_line",
    "install_pth",
    "remove_pth",
]
