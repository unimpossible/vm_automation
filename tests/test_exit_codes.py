"""Coverage 3-4: reserved exit codes -- 125 (can't connect) and 124 (timeout), fast."""

import time


def test_unreachable_host_exits_125_fast(run_vm, bad_host_config):
    """Pointing --config at an unroutable host must fail with rc 125 within ~15s."""
    start = time.time()
    rc, out, err = run_vm(["run", "true"], config=bad_host_config, timeout=30)
    elapsed = time.time() - start
    assert rc == 125, "expected connect-failure rc 125, got %d (stderr=%r)" % (rc, err)
    assert elapsed < 15, "connect failure took too long: %.1fs" % elapsed
    assert err.strip(), "expected a clear stderr error line on connect failure"


def test_run_timeout_exits_124(run_vm):
    """`run "sleep 30" --timeout 2` -> rc 124 (timeout) within a few seconds."""
    start = time.time()
    rc, out, err = run_vm(["run", "sleep 30", "--timeout", "2"], timeout=30)
    elapsed = time.time() - start
    assert rc == 124, "expected timeout rc 124, got %d (stderr=%r)" % (rc, err)
    assert elapsed < 15, "timeout took too long to fire: %.1fs" % elapsed
