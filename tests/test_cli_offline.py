"""Offline CLI behavior -- argument/config error paths that need no VM at all.

These are the tests CI actually exercises (marked `offline`); everything else
in the suite drives the live VM and skips when it is unreachable.
"""

import json
import os
import subprocess
import sys

import pytest

from conftest import VM_MODULE, _REPO_ROOT


def _run_cli(args, timeout=30):
    """Invoke `python -m vm_cli.cli <args...>` with no implied config/vm flags."""
    cmd = [sys.executable, "-m", VM_MODULE] + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=_REPO_ROOT)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


@pytest.mark.offline
def test_help_exits_zero():
    rc, out, err = _run_cli(["--help"])
    assert rc == 0
    text = out + err
    assert "run" in text and "push" in text, "help should list the core verbs"


@pytest.mark.offline
def test_missing_config_exits_125_with_clear_error(tmp_path):
    rc, out, err = _run_cli(["--config", str(tmp_path / "nope.json"), "run", "true"])
    assert rc == 125, "missing config should exit 125, got %d" % rc
    assert "config file not found" in err


@pytest.mark.offline
def test_malformed_config_exits_125(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    rc, out, err = _run_cli(["--config", str(bad), "run", "true"])
    assert rc == 125, "malformed config should exit 125, got %d" % rc
    assert err.strip(), "expected a clear stderr error line"


@pytest.mark.offline
def test_unknown_vm_exits_125_and_lists_known(tmp_path):
    cfg = {"vms": {"somevm": {"os": "linux", "host": "10.255.255.1",
                              "default_user": "user",
                              "users": {"user": {"password": "x"}}}}}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    rc, out, err = _run_cli(["--config", str(path), "--vm", "doesnotexist", "run", "true"])
    assert rc == 125, "unknown VM should exit 125, got %d" % rc
    assert "doesnotexist" in err and "somevm" in err, "error should name the bad and known VMs"
