"""Coverage 1-2: `run` verb -- basic exec, sudo `--as` path, exit-code passthrough."""


def test_run_id_reports_user(run_vm):
    """`run "id"` -> rc 0 and stdout contains uid=1000(user)."""
    rc, out, err = run_vm(["run", "id"])
    assert rc == 0, "expected rc 0, got %d (stderr=%r)" % (rc, err)
    assert "uid=1000(user)" in out, "stdout did not contain uid=1000(user): %r" % out


def test_run_id_as_user_via_sudo(run_vm):
    """`run "id" --as user` exercises the piped `sudo -S -u user` path; still uid 1000."""
    rc, out, err = run_vm(["run", "id", "--as", "user"])
    assert rc == 0, "expected rc 0, got %d (stderr=%r)" % (rc, err)
    assert "uid=1000(user)" in out, "stdout did not contain uid=1000(user): %r" % out


def test_run_exit_code_passthrough(run_vm):
    """`run "exit 7"` -> the remote rc (7) passes straight through."""
    rc, out, err = run_vm(["run", "exit 7"])
    assert rc == 7, "expected remote rc 7 to pass through, got %d" % rc
