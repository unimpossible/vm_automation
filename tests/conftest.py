"""Shared fixtures/helpers for the vm CLI live-integration suite.

Live tests drive whichever Linux VM vmconfig.json defines (default_vm if it's
Linux, else the first Linux entry) by invoking the CLI as a subprocess, exactly
as an agent would. Nothing VM-specific is hardcoded; without a config (CI),
offline-marked tests run against a synthesized config and the rest skip.

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
# Invoke the CLI as a module so it exercises the installed-style entry path.
VM_MODULE = "vm_cli.cli"
DEFAULT_CONFIG = os.path.join(_REPO_ROOT, "vmconfig.json")
# Neutral name used when no real config exists (CI / offline runs).
FALLBACK_VM = "civm"


def _load_real_config():
    try:
        with open(DEFAULT_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _pick_linux_vm(cfg):
    """Pick default_vm if it's Linux, else the first Linux VM in config order."""
    vms = (cfg or {}).get("vms") or {}
    default = cfg.get("default_vm") if cfg else None
    candidates = ([default] if default in vms else []) + [n for n in vms if n != default]
    for name in candidates:
        if (vms[name].get("os") or "linux").strip().lower() == "linux":
            return name
    return None


_REAL_CFG = _load_real_config()
VM_NAME = _pick_linux_vm(_REAL_CFG) or FALLBACK_VM
_VM_BLOCK = ((_REAL_CFG or {}).get("vms") or {}).get(VM_NAME) or {}
EXPECTED_HOST = _VM_BLOCK.get("host")  # None when no real config (CI)
# Basename of the VM's .vmx, e.g. "myvm.vmx" — what `vm list` prints per running VM.
EXPECTED_VMX = os.path.basename(_VM_BLOCK["vmx"]) if _VM_BLOCK.get("vmx") else None

# Guest-side write confinement root (a unique run-<uuid> subdir is created below it).
CONFINE_BASE = "/home/%s/Desktop/vm_automation_test" % (_VM_BLOCK.get("default_user") or "user")


def _run_vm(args, config=None, timeout=60):
    """Invoke `python -m vm_cli.cli --config <cfg> --vm <picked-vm> <args...>`.

    Returns (returncode, stdout_text, stderr_text). Bytes are decoded as UTF-8
    with replacement so callers can substring-match freely. The global --config
    and --vm options must precede the verb (they live on the top-level parser).
    """
    cfg = config or DEFAULT_CONFIG
    cmd = [sys.executable, "-m", VM_MODULE, "--config", cfg, "--vm", VM_NAME] + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=_REPO_ROOT)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


@pytest.fixture(scope="session")
def run_vm():
    """Expose the subprocess helper as a fixture: run_vm([...], config=..., timeout=...)."""
    return _run_vm


@pytest.fixture(scope="session")
def _vm_probe():
    """Probe the live VM once per session. Returns None if reachable, else a skip reason."""
    if not _VM_BLOCK:
        return ("no Linux VM found in %s (live tests need a real config "
                "with at least one Linux VM)" % os.path.basename(DEFAULT_CONFIG))
    try:
        rc, out, err = _run_vm(["run", "true"], timeout=25)
    except subprocess.TimeoutExpired:
        return "VM %s unreachable at session start (SSH probe timed out)" % VM_NAME
    except Exception as e:  # pragma: no cover - defensive
        return "VM %s unreachable at session start: %s" % (VM_NAME, e)
    if rc != 0:
        return (
            "VM %s unreachable at session start (rc=%d): %s"
            % (VM_NAME, rc, err.strip() or out.strip())
        )
    return None


@pytest.fixture(autouse=True)
def _require_vm(request):
    """Skip live tests when the VM is unreachable; tests marked `offline` always run."""
    if request.node.get_closest_marker("offline"):
        return
    reason = request.getfixturevalue("_vm_probe")
    if reason:
        pytest.skip(reason)


@pytest.fixture(scope="session")
def run_dir(run_vm, _vm_probe):
    """Create a unique confined guest directory for the session; remove it on teardown.

    Yields the remote POSIX path, e.g. /home/user/Desktop/vm_automation_test/run-<uuid>.
    Every guest-side file a test creates must live under this path.

    Depends on _vm_probe explicitly: as a session fixture it is set up before the
    function-scoped _require_vm guard, so it must skip (not error) on its own.
    """
    if _vm_probe:
        pytest.skip(_vm_probe)
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
    Mirrors the real config when present; otherwise (e.g. in CI, where no
    vmconfig.json exists) synthesizes a minimal config so the test still runs.
    """
    cfg = None
    if os.path.exists(DEFAULT_CONFIG):
        with open(DEFAULT_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if VM_NAME not in (cfg.get("vms") or {}):
            cfg = None
    if cfg is None:
        cfg = {
            "vms": {
                VM_NAME: {
                    "os": "linux",
                    "default_user": "user",
                    "users": {"user": {"password": "x"}},
                }
            }
        }
    cfg["vms"][VM_NAME]["host"] = "10.255.255.1"  # unroutable -> fast TCP failure
    path = tmp_path / "bad_host_config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)
