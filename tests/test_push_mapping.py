"""Offline unit tests for push source expansion / remote-path mapping.

These exercise the pure functions behind `vm push` (glob/dir/wildcard handling
and how a local source maps to its remote-relative path) with no VM at all.
"""

import os
import sys

import pytest

from conftest import _REPO_ROOT

sys.path.insert(0, _REPO_ROOT)
from vm_cli import cli  # noqa: E402


@pytest.mark.offline
def test_rel_component_preserves_relative_dirs():
    # A typed relative path keeps its structure (so docs/ is recreated remotely).
    assert cli._rel_component("docs/a.txt") == "docs/a.txt"
    assert cli._rel_component("docs\\a.txt") == "docs/a.txt"   # windows sep
    assert cli._rel_component("./docs/a.txt") == "docs/a.txt"  # leading ./
    assert cli._rel_component("a.txt") == "a.txt"
    assert cli._rel_component("pkg/mod/util.py") == "pkg/mod/util.py"


@pytest.mark.offline
def test_rel_component_flattens_absolute_and_escaping():
    # Absolute / drive-qualified / .. paths fall back to basename (no host layout leak).
    assert cli._rel_component("/etc/hosts") == "hosts"
    assert cli._rel_component("C:/Users/me/notes/a.txt") == "a.txt"
    assert cli._rel_component("C:\\Users\\me\\a.txt") == "a.txt"
    assert cli._rel_component("../sibling/a.txt") == "a.txt"


@pytest.mark.offline
def test_expand_sources_glob(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("a")
    (tmp_path / "docs" / "b.txt").write_text("b")
    (tmp_path / "docs" / "readme.md").write_text("m")
    monkeypatch.chdir(tmp_path)

    pairs = cli._expand_sources(["docs/*.txt"])
    comps = sorted(c for _lf, c in pairs)
    assert comps == ["docs/a.txt", "docs/b.txt"], comps  # .md excluded, docs/ kept


@pytest.mark.offline
def test_expand_sources_directory_recursive(tmp_path, monkeypatch):
    (tmp_path / "pkg" / "mod").mkdir(parents=True)
    (tmp_path / "pkg" / "main.py").write_text("1")
    (tmp_path / "pkg" / "mod" / "util.py").write_text("2")
    monkeypatch.chdir(tmp_path)

    pairs = cli._expand_sources(["pkg"])
    comps = sorted(c for _lf, c in pairs)
    assert comps == ["pkg/main.py", "pkg/mod/util.py"], comps


@pytest.mark.offline
def test_expand_sources_no_glob_match_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli._expand_sources(["docs/*.xyz"])
    assert e.value.code == cli.EXIT_ENV


@pytest.mark.offline
def test_expand_sources_missing_literal_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli._expand_sources(["nope.txt"])
    assert e.value.code == cli.EXIT_ENV
