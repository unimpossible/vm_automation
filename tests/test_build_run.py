"""Coverage 6: build-run -- C compile-and-run, --args (REMAINDER, last), .sh source, --keep.

Note on --args: it is argparse.REMAINDER, so it MUST be the final flag on the command
line and swallows everything after it.
"""

C_UID = """\
#include <stdio.h>
#include <unistd.h>
int main(void) {
    printf("uid=%d\\n", getuid());
    return 0;
}
"""

C_ARGS = """\
#include <stdio.h>
int main(int argc, char **argv) {
    if (argc > 1) printf("arg1=%s\\n", argv[1]);
    if (argc > 2) printf("arg2=%s\\n", argv[2]);
    return 0;
}
"""

SH_SRC = "#!/bin/bash\necho shell-ran-ok\nexit 0\n"


def test_build_run_c_prints_uid(run_vm, tmp_path):
    """A ~5-line C program compiles on the VM, runs, and prints getuid() == 1000."""
    src = tmp_path / "uid.c"
    src.write_text(C_UID)
    rc, out, err = run_vm(["build-run", str(src)], timeout=90)
    assert rc == 0, "build-run rc=%d stderr=%r" % (rc, err)
    assert "uid=1000" in out, "expected uid=1000 in program output, got %r" % out


def test_build_run_c_with_args(run_vm, tmp_path):
    """--args (REMAINDER) is passed to the compiled program; must be the last flag."""
    src = tmp_path / "args.c"
    src.write_text(C_ARGS)
    rc, out, err = run_vm(["build-run", str(src), "--args", "hello", "world"], timeout=90)
    assert rc == 0, "build-run rc=%d stderr=%r" % (rc, err)
    assert "arg1=hello" in out, "missing arg1 in output: %r" % out
    assert "arg2=world" in out, "missing arg2 in output: %r" % out


def test_build_run_shell_source(run_vm, tmp_path):
    """A .sh source skips compilation and is run via bash."""
    src = tmp_path / "hello.sh"
    src.write_text(SH_SRC)
    rc, out, err = run_vm(["build-run", str(src)], timeout=60)
    assert rc == 0, "build-run .sh rc=%d stderr=%r" % (rc, err)
    assert "shell-ran-ok" in out, "expected shell output, got %r" % out


def test_build_run_keep_leaves_tempdir(run_vm, tmp_path):
    """--keep leaves the /tmp/vmbuild.XXXXXX dir; we assert it, then clean it up ourselves."""
    src = tmp_path / "keep.c"
    src.write_text(C_UID)
    rc, out, err = run_vm(["build-run", str(src), "--keep"], timeout=90)
    assert rc == 0, "build-run --keep rc=%d stderr=%r" % (rc, err)

    # vm.py prints "kept build dir: <workdir>" to stderr when --keep is set.
    workdir = None
    for line in err.splitlines():
        marker = "kept build dir:"
        if marker in line:
            workdir = line.split(marker, 1)[1].strip()
            break
    assert workdir, "expected a 'kept build dir:' line on stderr, got %r" % err
    assert workdir.startswith("/tmp/vmbuild."), "unexpected build dir path: %r" % workdir

    try:
        rc2, out2, err2 = run_vm(["run", "test -d %s" % workdir], timeout=30)
        assert rc2 == 0, "kept build dir %s does not exist (rc=%d)" % (workdir, rc2)
    finally:
        # Be tidy: remove the kept dir even though it lives under /tmp (build-run's own space).
        run_vm(["run", "rm -rf %s" % workdir], timeout=30)
