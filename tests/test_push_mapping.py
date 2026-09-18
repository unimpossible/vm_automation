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
def test_win_ps_b64_decode_cmd_is_shell_safe_and_decodable():
    import base64

    cmd = cli._win_ps_b64_decode_cmd("c:/eec_injest_poc/win_forge_core_metric.ps1")
    # cmd.exe never has to quote it: prefix + pure-base64 payload, no spaces after.
    prefix = "powershell -NoProfile -NonInteractive -EncodedCommand "
    assert cmd.startswith(prefix)
    payload = cmd[len(prefix):]
    assert " " not in payload

    # The payload round-trips to a UTF-16LE PowerShell script targeting the dest.
    script = base64.b64decode(payload).decode("utf-16-le")
    assert "FromBase64String" in script
    assert r"c:\eec_injest_poc\win_forge_core_metric.ps1" in script  # / -> \
    # Parent dir is only created when absent, so a drive-root parent doesn't throw.
    assert "Test-Path -LiteralPath $dir" in script


@pytest.mark.offline
def test_win_ps_b64_decode_cmd_escapes_quotes():
    cmd = cli._win_ps_b64_decode_cmd("c:/od'd/x.ps1")
    script = __import__("base64").b64decode(
        cmd.rsplit(" ", 1)[-1]).decode("utf-16-le")
    assert "$dst='c:\\od''d\\x.ps1'" in script  # embedded ' doubled for PowerShell


@pytest.mark.offline
def test_remote_is_dir_via_sftp():
    import stat as _stat

    class _FakeSftp:
        def __init__(self, mode):
            self._mode = mode

        def stat(self, path):
            if self._mode is None:
                raise IOError("no such file")
            return type("S", (), {"st_mode": self._mode})()

    dir_sftp = _FakeSftp(_stat.S_IFDIR | 0o755)
    file_sftp = _FakeSftp(_stat.S_IFREG | 0o644)
    missing_sftp = _FakeSftp(None)

    assert cli._remote_is_dir(None, {}, dir_sftp, "c:/eec_injest_poc") is True
    assert cli._remote_is_dir(None, {}, file_sftp, "c:/eec_injest_poc") is False
    assert cli._remote_is_dir(None, {}, missing_sftp, "c:/nope") is False
    assert cli._remote_is_dir(None, {}, dir_sftp, "") is False  # empty path


@pytest.mark.offline
def test_strip_top_component_maps_dir_contents_to_root():
    # Files under the source dir lose the dir's own name (contents -> dest root).
    assert cli._strip_top_component("files/file1.txt", "files") == "file1.txt"
    assert cli._strip_top_component("files/dir/extra.txt", "files") == "dir/extra.txt"
    # The bare top (empty-dir marker) collapses to "" (== the dest root itself).
    assert cli._strip_top_component("files", "files") == ""
    # A component that doesn't start with top/ is passed through unchanged.
    assert cli._strip_top_component("other/x.txt", "files") == "other/x.txt"


@pytest.mark.offline
def test_single_dir_push_maps_contents_to_dest_root(tmp_path, monkeypatch):
    # `vm push files c:\files` must land contents directly under dest, NOT under
    # dest/files. We verify the (local, remote-relative) mapping cmd_push builds.
    (tmp_path / "files" / "dirtemp").mkdir(parents=True)
    (tmp_path / "files" / "file1.txt").write_text("1")
    (tmp_path / "files" / "file2.txt").write_text("2")
    (tmp_path / "files" / "dirtemp" / "extra.txt").write_text("3")
    monkeypatch.chdir(tmp_path)

    top = cli._rel_component("files")  # "files"
    pairs = cli._expand_sources(["files"])
    mapped = sorted(cli._strip_top_component(comp, top) for _lf, comp in pairs)
    assert mapped == ["dirtemp/extra.txt", "file1.txt", "file2.txt"], mapped


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
