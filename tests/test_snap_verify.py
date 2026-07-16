"""Coverage 7: stateless snap/verify -- MODIFIED/UNCHANGED/CREATED, --token, parallel safety.

vm.py's verify prints exactly `RESULT` and, when --token is given, `RESULT TOKEN=present|absent`.
The baseline is carried by the caller (a snap-line), so there is no shared state to race on.
"""


def test_verify_modified_with_token_present(run_vm, run_dir):
    path = "%s/mod.txt" % run_dir
    rc, _, err = run_vm(["run", "printf 'initial\\n' > %s" % path], timeout=30)
    assert rc == 0, err

    rc, baseline_out, err = run_vm(["snap", path], timeout=30)
    assert rc == 0, err
    baseline = baseline_out.strip()
    assert baseline and baseline != "MISSING", "unexpected baseline: %r" % baseline

    # modify the file, appending a distinctive token
    rc, _, err = run_vm(["run", "printf 'MODTOKEN42\\n' >> %s" % path], timeout=30)
    assert rc == 0, err

    rc, out, err = run_vm(
        ["verify", path, "--baseline", baseline, "--token", "MODTOKEN42"], timeout=30
    )
    assert rc == 0, err
    assert out.strip() == "MODIFIED TOKEN=present", "got %r" % out


def test_verify_unchanged(run_vm, run_dir):
    path = "%s/unchanged.txt" % run_dir
    rc, _, err = run_vm(["run", "printf 'stable content\\n' > %s" % path], timeout=30)
    assert rc == 0, err

    rc, baseline_out, err = run_vm(["snap", path], timeout=30)
    assert rc == 0, err
    baseline = baseline_out.strip()

    # do NOT modify; verify against the same baseline
    rc, out, err = run_vm(["verify", path, "--baseline", baseline], timeout=30)
    assert rc == 0, err
    assert out.strip() == "UNCHANGED", "got %r" % out


def test_verify_created(run_vm, run_dir):
    path = "%s/created.txt" % run_dir
    # snap a not-yet-existing path -> MISSING baseline
    rc, baseline_out, err = run_vm(["snap", path], timeout=30)
    assert rc == 0, err
    baseline = baseline_out.strip()
    assert baseline == "MISSING", "expected MISSING for absent file, got %r" % baseline

    # now create it, and verify against the MISSING baseline -> CREATED
    rc, _, err = run_vm(["run", "printf 'now here\\n' > %s" % path], timeout=30)
    assert rc == 0, err
    rc, out, err = run_vm(["verify", path, "--baseline", baseline], timeout=30)
    assert rc == 0, err
    assert out.strip() == "CREATED", "got %r" % out


def test_two_baselines_do_not_interfere(run_vm, run_dir):
    """Parallel-safety: two independent files, each verified with its OWN baseline."""
    path_a = "%s/parA.txt" % run_dir
    path_b = "%s/parB.txt" % run_dir
    rc, _, err = run_vm(["run", "printf 'alpha\\n' > %s" % path_a], timeout=30)
    assert rc == 0, err
    rc, _, err = run_vm(["run", "printf 'bravo\\n' > %s" % path_b], timeout=30)
    assert rc == 0, err

    rc, base_a_out, err = run_vm(["snap", path_a], timeout=30)
    assert rc == 0, err
    base_a = base_a_out.strip()
    rc, base_b_out, err = run_vm(["snap", path_b], timeout=30)
    assert rc == 0, err
    base_b = base_b_out.strip()
    assert base_a != base_b, "distinct files should yield distinct snap lines"

    # modify each with its own token
    rc, _, err = run_vm(["run", "printf 'TOKA\\n' >> %s" % path_a], timeout=30)
    assert rc == 0, err
    rc, _, err = run_vm(["run", "printf 'TOKB\\n' >> %s" % path_b], timeout=30)
    assert rc == 0, err

    rc, out_a, err = run_vm(
        ["verify", path_a, "--baseline", base_a, "--token", "TOKA"], timeout=30
    )
    assert rc == 0, err
    assert out_a.strip() == "MODIFIED TOKEN=present", "A: got %r" % out_a

    rc, out_b, err = run_vm(
        ["verify", path_b, "--baseline", base_b, "--token", "TOKB"], timeout=30
    )
    assert rc == 0, err
    assert out_b.strip() == "MODIFIED TOKEN=present", "B: got %r" % out_b

    # cross-check independence: A's token must NOT be reported present in B
    rc, out_x, err = run_vm(
        ["verify", path_b, "--baseline", base_b, "--token", "TOKA"], timeout=30
    )
    assert rc == 0, err
    assert out_x.strip() == "MODIFIED TOKEN=absent", "cross-check: got %r" % out_x
