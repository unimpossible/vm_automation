"""Shared fixtures/helpers for the vm.py live-integration suite.

All tests drive the real, already-running VM "ubuntu24" (192.168.187.130, user:user)
by invoking vm.py as a subprocess, exactly as an agent would.

Safety rules baked in here:
  * Guest-side writes are confined to /home/user/Desktop/vm_automation_test/run-<uuid>/,
    created once per session and removed on teardown.
  * Only READ-ONLY host verbs (vm list / snapshots / ip) are exercised elsewhere.
  * A session-scoped connectivity probe skips the WHOLE suite (with a clear reason)
    when the VM is unreachable, instead of producing dozens of hard failures.
"""

import json
import os
import subprocess
import sys
import uuid

import pytest

# Tool under test and its real config live in the repo root (this file's parent).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM_PY = os.path.join(_REPO_ROOT, "vm.py")
DEFAULT_CONFIG = os.path.join(_REPO_ROOT, "vmconfig.json")
VM_NAME = "ubuntu24"
EXPECTED_HOST = "192.168.187.130"

# Guest-side write confinement root (a unique run-<uuid> subdir is created below it).
CONFINE_BASE = "/home/user/Desktop/vm_automation_test"


def _run_vm(args, config=None, timeout=60):
    """Invoke `python vm.py --config <cfg> --vm ubuntu24 <args...>`.

    Returns (returncode, stdout_text, stderr_text). Bytes are decoded as UTF-8
    with replacement so callers can substring-match freely. The global --config
    and --vm options must precede the verb (they live on the top-level parser).
    """
    cfg = config or DEFAULT_CONFIG
    cmd = [sys.executable, VM_PY, "--config", cfg, "--vm", VM_NAME] + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


@pytest.fixture(scope="session")
def run_vm():
    """Expose the subprocess helper as a fixture: run_vm([...], config=..., timeout=...)."""
    return _run_vm


@pytest.fixture(scope="session", autouse=True)
def _require_vm():
    """Skip the entire suite up front if the live VM can't be reached over SSH."""
    try:
        rc, out, err = _run_vm(["run", "true"], timeout=25)
    except subprocess.TimeoutExpired:
        pytest.skip("VM %s unreachable at session start (SSH probe timed out)" % VM_NAME)
        return
    except Exception as e:  # pragma: no cover - defensive
        pytest.skip("VM %s unreachable at session start: %s" % (VM_NAME, e))
        return
    if rc != 0:
        pytest.skip(
            "VM %s unreachable at session start (rc=%d): %s"
            % (VM_NAME, rc, err.strip() or out.strip())
        )


@pytest.fixture(scope="session")
def run_dir(run_vm):
    """Create a unique confined guest directory for the session; remove it on teardown.

    Yields the remote POSIX path, e.g. /home/user/Desktop/vm_automation_test/run-<uuid>.
    Every guest-side file a test creates must live under this path.
    """
    remote = "%s/run-%s" % (CONFINE_BASE, uuid.uuid4().hex)
    rc, out, err = run_vm(["run", "mkdir -p %s" % remote], timeout=30)
    assert rc == 0, "could not create confined run dir %s: %s" % (remote, err)
    yield remote
    # Best-effort cleanup; don't fail teardown if the VM went away.
    try:
        run_vm(["run", "rm -rf %s" % remote], timeout=30)
    except Exception:
        pass


@pytest.fixture()
def bad_host_config(tmp_path):
    """Write a temp config identical to the real one but pointing at an unroutable IP.

    Used to force a fast connect failure (exit 125) without touching the real VM.
    """
    with open(DEFAULT_CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["vms"][VM_NAME]["host"] = "10.255.255.1"  # unroutable -> fast TCP failure
    path = tmp_path / "bad_host_config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)
