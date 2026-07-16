"""Coverage 8: waitfile -- returns 0 when a file appears, 124 when it never does."""

import subprocess
import sys
import time

from conftest import VM_PY, DEFAULT_CONFIG, VM_NAME


def test_waitfile_returns_0_when_file_appears(run_vm, run_dir):
    """A background run touches the file after a short sleep; waitfile must return 0."""
    path = "%s/appears.txt" % run_dir

    # Launch a detached `run` that sleeps then touches the file (non-blocking on the host).
    toucher = subprocess.Popen(
        [sys.executable, VM_PY, "--config", DEFAULT_CONFIG, "--vm", VM_NAME,
         "run", "sleep 3; touch %s" % path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        rc, out, err = run_vm(["waitfile", path, "--timeout", "25"], timeout=40)
        assert rc == 0, "waitfile should return 0 when file appears; got %d (%r)" % (rc, err)
    finally:
        try:
            toucher.wait(timeout=30)
        except Exception:
            toucher.kill()


def test_waitfile_times_out_124(run_vm, run_dir):
    """waitfile on a path that never appears -> rc 124 within a few seconds."""
    path = "%s/never.txt" % run_dir
    start = time.time()
    rc, out, err = run_vm(["waitfile", path, "--timeout", "2"], timeout=30)
    elapsed = time.time() - start
    assert rc == 124, "expected timeout rc 124, got %d (stderr=%r)" % (rc, err)
    assert elapsed < 12, "waitfile timeout took too long: %.1fs" % elapsed
