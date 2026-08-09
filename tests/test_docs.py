"""`vm docs` -- the README/SKILL.md reference an agent can read after a pip install.

The originals are tracked once at the repo root; setup.py stages copies into
vm_cli/_docs/ when building a wheel. These tests cover the source-tree side
(resolution falls back to the root originals) and the shipped-wheel side
(the build actually stages them).
"""

import os
import subprocess
import sys

import pytest

from conftest import VM_MODULE, _REPO_ROOT

DOC_NAMES = ["README.md", "SKILL.md"]


def _run_cli(args, cwd, timeout=30):
    """Run the CLI from `cwd` -- PYTHONPATH keeps vm_cli importable from anywhere."""
    cmd = [sys.executable, "-m", VM_MODULE] + list(args)
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(cwd), env=env)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@pytest.mark.offline
@pytest.mark.parametrize("name", DOC_NAMES)
def test_docs_are_tracked_once_at_repo_root(name):
    assert os.path.isfile(os.path.join(_REPO_ROOT, name)), "missing original: %s" % name
    staged = os.path.join(_REPO_ROOT, "vm_cli", "_docs", name)
    if os.path.isfile(staged):  # a local build staged it; must match, never diverge
        assert _read(staged) == _read(os.path.join(_REPO_ROOT, name)), (
            "vm_cli/_docs/%s is a stale build artifact -- it is generated, not edited" % name)


@pytest.mark.offline
def test_docs_prints_readme_without_config(tmp_path):
    """Must work from a directory with no vmconfig.json -- e.g. a fresh install."""
    rc, out, err = _run_cli(["docs"], cwd=tmp_path)
    assert rc == 0, err
    assert "## Verbs" in out


@pytest.mark.offline
def test_docs_skill_and_path(tmp_path):
    rc, out, _ = _run_cli(["docs", "--skill"], cwd=tmp_path)
    assert rc == 0
    assert "name: vm-recovery" in out

    rc, out, _ = _run_cli(["docs", "--path"], cwd=tmp_path)
    assert rc == 0
    assert os.path.isfile(out.strip())


@pytest.mark.offline
def test_docs_install_skill(tmp_path):
    rc, _, err = _run_cli(["docs", "--install-skill"], cwd=tmp_path)
    assert rc == 0, err
    dest = tmp_path / ".claude" / "skills" / "vm-recovery" / "SKILL.md"
    assert dest.is_file()
    assert "name: vm-recovery" in _read(str(dest))


@pytest.mark.offline
def test_wheel_ships_the_docs(tmp_path):
    """The build hook must stage both docs into the wheel -- a pip install has no repo."""
    import zipfile

    p = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(tmp_path),
                        _REPO_ROOT], capture_output=True, timeout=300)
    if p.returncode != 0:
        pytest.skip("wheel build unavailable: %s" % p.stderr.decode("utf-8", "replace")[-200:])
    wheels = [f for f in os.listdir(str(tmp_path)) if f.endswith(".whl")]
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(str(tmp_path / wheels[0])) as z:
        names = z.namelist()
        for name in DOC_NAMES:
            member = "vm_cli/_docs/%s" % name
            assert member in names, "wheel is missing %s (got %s)" % (member, names)
            assert z.read(member).decode("utf-8") == _read(os.path.join(_REPO_ROOT, name))
