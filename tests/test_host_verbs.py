"""Coverage 9-10: doctor + READ-ONLY host verbs (ip / list / snapshots).

STRICT: only read-only host verbs are exercised here. This suite never calls
revert/reset/stop/start/snapshot against the user's live VM.
"""

from conftest import EXPECTED_HOST, EXPECTED_VMX


def _passed_lines(stdout):
    """Return the [PASS] report lines (label + detail) from a doctor report."""
    return [ln for ln in stdout.splitlines() if ln.lstrip().startswith("[PASS]")]


def test_doctor_all_pass(run_vm):
    """`vm doctor` -> rc 0 with PASS lines for config, vmrun, vmx, SSH, sudo."""
    rc, out, err = run_vm(["vm", "doctor"], timeout=90)
    assert rc == 0, "doctor rc=%d\nstdout=%s\nstderr=%s" % (rc, out, err)
    passed = _passed_lines(out)
    # Each label comes straight from vm.py's checks list; a PASS line carries
    # the label plus a padded detail column, so match the label as a substring.
    expected = [
        "config parses",
        "vmrun.exe exists",
        "vmx exists",
        "ssh connects",
        "sudo as user",
    ]
    missing = [lbl for lbl in expected if not any(lbl in ln for ln in passed)]
    assert not missing, "doctor missing PASS for %s\nfull report:\n%s" % (missing, out)


def test_vm_ip_reports_expected_host(run_vm):
    """`vm ip` -> rc 0 and stdout contains the expected guest IP."""
    rc, out, err = run_vm(["vm", "ip"], timeout=90)
    assert rc == 0, "vm ip rc=%d stderr=%r" % (rc, err)
    assert EXPECTED_HOST in out, "expected %s in vm ip output, got %r" % (EXPECTED_HOST, out)


def test_vm_list_shows_vmx(run_vm):
    """`vm list` -> rc 0 and the running inventory includes the picked VM's .vmx."""
    rc, out, err = run_vm(["vm", "list"], timeout=60)
    assert rc == 0, "vm list rc=%d stderr=%r" % (rc, err)
    assert EXPECTED_VMX, "config has no vmx for the picked VM"
    assert EXPECTED_VMX in out, "%s not listed as running; got %r" % (EXPECTED_VMX, out)


def test_vm_snapshots_runs(run_vm):
    """`vm snapshots` -> rc 0 (an empty snapshot list is tolerated)."""
    rc, out, err = run_vm(["vm", "snapshots"], timeout=60)
    assert rc == 0, "vm snapshots rc=%d stderr=%r" % (rc, err)
