# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install/uninstall lifecycle: .pth site contract, content guard, and
install_pth/remove_pth round-trip (idempotent)."""

from __future__ import annotations

import builtins
import site

import agentarts_instrumentation.openjiuwen.inject as inj


def test_pth_line_starts_with_import():
    """site.addpackage only executes .pth lines that start with "import "
    — anything else is (mis)treated as a sys.path entry."""
    assert inj._PTH_LINE.startswith("import ")


def test_pth_line_is_valid_python():
    """The one-liner (including its exec-wrapped guard) must compile."""
    compile(inj._PTH_LINE, "<pth>", "exec")


def test_pth_line_guards_missing_package(monkeypatch):
    """The guard must swallow ModuleNotFoundError silently: after a
    package uninstall that leaves the .pth behind, interpreter starts
    stay quiet instead of printing tracebacks."""

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        # Simulate the uninstalled package; delegate everything else
        # (incl. `import sys` and pytest internals) to the real import.
        if name == "agentarts_instrumentation" or name.startswith(
            "agentarts_instrumentation."
        ):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    exec(inj._PTH_LINE, {"__builtins__": __builtins__})  # must not raise


def test_install_remove_roundtrip(tmp_path, monkeypatch, capsys):
    """install_pth writes the .pth next to the package (idempotently,
    upgrading older content); remove_pth deletes it (idempotently)."""
    pkg_dir = tmp_path / "agentarts_instrumentation" / "openjiuwen"
    pkg_dir.mkdir(parents=True)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path)])

    pth = tmp_path / "agentarts_instrumentation_openjiuwen_inject.pth"

    # Fresh install writes the current content.
    assert inj.install_pth() == 0
    assert pth.read_text(encoding="utf-8") == inj._PTH_LINE
    assert "installed" in capsys.readouterr().out

    # Second install with identical content is a no-op.
    assert inj.install_pth() == 0
    assert "already installed" in capsys.readouterr().out
    assert pth.read_text(encoding="utf-8") == inj._PTH_LINE

    # Older (unguarded) content gets upgraded in place.
    pth.write_text("import agentarts_instrumentation.openjiuwen.inject\n", encoding="utf-8")
    assert inj.install_pth() == 0
    assert "upgraded" in capsys.readouterr().out
    assert pth.read_text(encoding="utf-8") == inj._PTH_LINE

    # Remove deletes and reports; a second remove is a clean no-op.
    assert inj.remove_pth() == 0
    assert "removed" in capsys.readouterr().out
    assert not pth.exists()
    assert inj.remove_pth() == 0
    assert "nothing to remove" in capsys.readouterr().out
