"""Coverage 5: push a local file, confirm byte count on the VM, pull it back, compare bytes."""

import os


def test_push_wc_pull_roundtrip(run_vm, run_dir, tmp_path):
    payload = b"hello vm.py\x00\x01\x02 binary-ish payload \xff\xfe end\n" * 7
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    remote = "%s/payload.bin" % run_dir

    # push local -> remote (SFTP with base64 fallback inside vm.py)
    rc, out, err = run_vm(["push", str(local), remote], timeout=60)
    assert rc == 0, "push failed rc=%d stderr=%r" % (rc, err)

    # byte count on the VM must match the local size
    rc, out, err = run_vm(["run", "wc -c %s" % remote], timeout=30)
    assert rc == 0, "wc -c failed rc=%d stderr=%r" % (rc, err)
    reported = int(out.split()[0])
    assert reported == len(payload), "remote byte count %d != local %d" % (reported, len(payload))

    # pull remote -> a fresh local file and compare bytes exactly
    pulled = tmp_path / "pulled.bin"
    rc, out, err = run_vm(["pull", remote, str(pulled)], timeout=60)
    assert rc == 0, "pull failed rc=%d stderr=%r" % (rc, err)
    assert pulled.read_bytes() == payload, "pulled bytes differ from original"
    assert os.path.getsize(str(pulled)) == len(payload)
