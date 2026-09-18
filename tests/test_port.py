"""Coverage: `port open|close|list` against the live Linux VM's firewall.

The round trip is self-cleaning -- it closes the port it opened in a finally --
and uses a port nothing else is expected to use, so it never leaves a rule behind.
"""

import pytest

PORT = 19099


def _open_ports(run_vm):
    """Return the list of '<port>/<proto>' lines `port list` prints on stdout."""
    rc, out, err = run_vm(["port", "list"], timeout=90)
    assert rc == 0, "port list rc=%d stderr=%r" % (rc, err)
    return out.split()


def test_port_list_runs(run_vm):
    """`port list` -> rc 0 and names the backend it used on stderr."""
    rc, out, err = run_vm(["port", "list"], timeout=90)
    assert rc == 0, "port list rc=%d stderr=%r" % (rc, err)
    assert "ufw" in err or "iptables" in err, "expected a backend in %r" % err


def test_port_open_close_round_trip(run_vm):
    """open -> the port shows in list -> close -> it's gone again."""
    assert "%d/tcp" % PORT not in _open_ports(run_vm), "test port is already open"
    rc, out, err = run_vm(["port", "open", str(PORT)], timeout=90)
    assert rc == 0, "port open rc=%d stderr=%r" % (rc, err)
    try:
        assert "%d/tcp" % PORT in _open_ports(run_vm), "opened port missing from list"
    finally:
        rc, out, err = run_vm(["port", "close", str(PORT)], timeout=90)
        assert rc == 0, "port close rc=%d stderr=%r" % (rc, err)
    assert "%d/tcp" % PORT not in _open_ports(run_vm), "closed port still listed"


def test_port_udp_is_separate_from_tcp(run_vm):
    """--proto udp opens udp only; the tcp rule is untouched."""
    rc, out, err = run_vm(["port", "open", str(PORT), "--proto", "udp"], timeout=90)
    assert rc == 0, "port open --proto udp rc=%d stderr=%r" % (rc, err)
    try:
        ports = _open_ports(run_vm)
        assert "%d/udp" % PORT in ports, "udp rule missing from list"
        assert "%d/tcp" % PORT not in ports, "udp open should not add a tcp rule"
    finally:
        run_vm(["port", "close", str(PORT), "--proto", "udp"], timeout=90)


def test_close_unopened_port_errors(run_vm):
    """Closing a rule that was never added fails loudly rather than silently."""
    rc, out, err = run_vm(["port", "close", str(PORT)], timeout=90)
    assert rc != 0, "closing a missing rule should fail, got rc=0"
    assert err.strip(), "expected a clear stderr error line"


def test_bad_port_number_rejected(run_vm):
    """Out-of-range ports exit 125 with the range in the message."""
    rc, out, err = run_vm(["port", "open", "99999"], timeout=90)
    assert rc == 125, "expected 125 for a bad port, got %d" % rc
    assert "1-65535" in err, "error should state the valid range: %r" % err
