#!/usr/bin/env python3
"""vm.py - Generic VM automation CLI (guest control over SSH + host control over VMware).

Single-file tool for driving test VMs from a Windows host, designed for low token overhead.

  Guest (SSH/paramiko):  run, push, pull, sync, build-run, snap, verify, waitfile
  Host  (vmrun.exe):     vm snapshot|revert|reset|start|stop|list|snapshots|ip|doctor
  Optional (WSL sshfs):  mount, umount

Usage:  python vm.py [--vm NAME] [--config PATH] <verb> ...

Exit codes:  remote rc passes through for `run`/`build-run`;
             124 = timeout, 125 = can't connect / config error / bad usage, 0 = success.
"""

import argparse
import base64
import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tempfile
import time

try:
    import paramiko
except ImportError:
    sys.stderr.write("error: paramiko is required (pip install paramiko)\n")
    sys.exit(125)

# --- Reserved exit codes -----------------------------------------------------
EXIT_TIMEOUT = 124
EXIT_ENV = 125  # can't connect / config error / bad usage

CONNECT_TIMEOUT = 10   # paramiko TCP/auth timeout (seconds)
DEFAULT_TIMEOUT = 120  # default remote command timeout (seconds)


def die(msg, code=EXIT_ENV):
    """Print one clear error line to stderr and exit with the given code."""
    sys.stderr.write("error: %s\n" % msg)
    sys.exit(code)


def status(msg):
    """Terse success/status line to stderr (keeps stdout for actual results)."""
    sys.stderr.write(msg + "\n")


# --- Config ------------------------------------------------------------------
def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        die("config file not found: %s" % path)
    except json.JSONDecodeError as e:
        die("config file is not valid JSON: %s" % e)


def resolve_vm(cfg, name):
    """Return (vm_name, vm_block) for the selected VM."""
    vms = cfg.get("vms") or {}
    if not vms:
        die("no VMs defined in config")
    if name is None:
        name = cfg.get("default_vm")
    if name is None:
        die("no --vm given and no default_vm in config")
    if name not in vms:
        die("unknown VM %r (known: %s)" % (name, ", ".join(sorted(vms))))
    return name, vms[name]


def atomic_write_json(path, data):
    """Write JSON atomically: temp file in same dir + os.replace."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".vmconfig.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def user_password(vm, user):
    users = vm.get("users") or {}
    if user not in users:
        die("user %r not configured for this VM" % user)
    return users[user].get("password", "")


# --- SSH ---------------------------------------------------------------------
def ssh_connect(vm):
    """Open a paramiko SSHClient to the VM's default_user. Fail fast on a dead VM."""
    host = vm.get("host")
    if not host:
        die("VM has no host/IP configured")
    user = vm.get("default_user")
    if not user:
        die("VM has no default_user configured")
    password = user_password(vm, user)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
    except Exception as e:
        die("cannot connect to %s@%s: %s" % (user, host, e), EXIT_ENV)
    return client


def _read_all(chan, timeout):
    """Drive a channel to completion within timeout; return (rc, stdout, stderr)."""
    chan.settimeout(1.0)
    out = bytearray()
    err = bytearray()
    deadline = time.time() + timeout
    while True:
        while chan.recv_ready():
            out += chan.recv(65536)
        while chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.time() > deadline:
            try:
                chan.close()
            except Exception:
                pass
            raise TimeoutError("command timed out after %ss" % timeout)
        time.sleep(0.05)
    # drain any remainder
    while chan.recv_ready():
        out += chan.recv(65536)
    while chan.recv_stderr_ready():
        err += chan.recv_stderr(65536)
    rc = chan.recv_exit_status()
    return rc, bytes(out), bytes(err)


def exec_command(client, vm, cmd, as_user=None, timeout=DEFAULT_TIMEOUT, stdin_data=None):
    """Run a command on the guest. --as USER wraps it in piped `sudo -S`.

    Returns (rc, stdout_bytes, stderr_bytes). Raises TimeoutError on timeout.
    """
    transport = client.get_transport()
    chan = transport.open_session()
    chan.settimeout(timeout)

    if as_user is not None:
        pw = user_password(vm, as_user)
        # feed password on stdin to `sudo -S`; run the command as that user's login shell
        wrapped = "sudo -S -p '' -u %s -- bash -c %s" % (
            shlex.quote(as_user), shlex.quote(cmd))
        chan.exec_command(wrapped)
        feed = (pw + "\n").encode()
        if stdin_data is not None:
            feed += stdin_data
        try:
            chan.sendall(feed)
        except Exception:
            pass
        chan.shutdown_write()
    else:
        chan.exec_command(cmd)
        if stdin_data is not None:
            try:
                chan.sendall(stdin_data)
            except Exception:
                pass
        chan.shutdown_write()

    return _read_all(chan, timeout)


# --- Guest verbs -------------------------------------------------------------
def cmd_run(client, vm, args):
    try:
        rc, out, err = exec_command(
            client, vm, args.command, as_user=args.as_user, timeout=args.timeout)
    except TimeoutError as e:
        die(str(e), EXIT_TIMEOUT)
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    return rc


def _sftp_or_none(client):
    try:
        return client.open_sftp()
    except Exception:
        return None


def _push_b64(client, vm, local, remote):
    """Fallback upload: base64 the local file, decode over exec on the guest."""
    with open(local, "rb") as f:
        data = f.read()
    b64 = base64.b64encode(data).decode()
    # ensure parent dir exists, then decode into place
    parent = posixpath.dirname(remote)
    prep = "mkdir -p %s && cat > /tmp/.vmpush.b64" % shlex.quote(parent or ".")
    rc, out, err = exec_command(client, vm, prep, timeout=DEFAULT_TIMEOUT,
                                stdin_data=b64.encode())
    if rc != 0:
        die("base64 push prep failed: %s" % err.decode(errors="replace"))
    rc, out, err = exec_command(
        client, vm,
        "base64 -d /tmp/.vmpush.b64 > %s && rm -f /tmp/.vmpush.b64" % shlex.quote(remote),
        timeout=DEFAULT_TIMEOUT)
    if rc != 0:
        die("base64 push decode failed: %s" % err.decode(errors="replace"))


def _push_file(client, vm, local, remote, sftp):
    """Upload one file; SFTP if available, base64-over-exec fallback. Returns method used."""
    if sftp is not None:
        try:
            parent = posixpath.dirname(remote)
            if parent:
                exec_command(client, vm, "mkdir -p %s" % shlex.quote(parent), timeout=30)
            sftp.put(local, remote)
            return "sftp"
        except Exception:
            pass
    _push_b64(client, vm, local, remote)
    return "base64"


def cmd_push(client, vm, args):
    # cp-style: `push SRC... DEST`. One positional = single file with a default remote;
    # two positionals = SRC DEST (a literal remote path); 3+ = SRC... DESTDIR (a directory).
    paths = args.paths
    if len(paths) == 1:
        locals_, dest, multi = paths, None, False
    else:
        locals_, dest, multi = paths[:-1], paths[-1], len(paths) > 2

    for lf in locals_:
        if not os.path.isfile(lf):
            die("local file not found: %s" % lf)

    sftp = _sftp_or_none(client)
    try:
        if not multi:
            local = locals_[0]
            remote = dest
            if not remote:
                # default: staging_remote/<basename>, else /tmp/<basename>
                staging = vm.get("staging_remote")
                base = os.path.basename(local)
                remote = posixpath.join(staging, base) if staging else "/tmp/" + base
            used = _push_file(client, vm, local, remote, sftp)
            status("pushed %s -> %s (%s)" % (local, remote, used))
        else:
            # multiple sources -> dest is a remote directory
            exec_command(client, vm, "mkdir -p %s" % shlex.quote(dest), timeout=30)
            for lf in locals_:
                remote = posixpath.join(dest, os.path.basename(lf))
                _push_file(client, vm, lf, remote, sftp)
            status("pushed %d file(s) -> %s" % (len(locals_), dest))
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
    return 0


def _pull_b64(client, vm, remote, local):
    rc, out, err = exec_command(
        client, vm, "base64 %s" % shlex.quote(remote), timeout=DEFAULT_TIMEOUT)
    if rc != 0:
        die("base64 pull failed: %s" % err.decode(errors="replace"))
    data = base64.b64decode(out)
    with open(local, "wb") as f:
        f.write(data)


def cmd_pull(client, vm, args):
    remote = args.remote
    local = args.local or os.path.basename(remote)
    if local and (os.path.isdir(local)):
        local = os.path.join(local, os.path.basename(remote))
    sftp = _sftp_or_none(client)
    used = "sftp"
    if sftp is not None:
        try:
            sftp.get(remote, local)
        except Exception:
            used = "base64"
            _pull_b64(client, vm, remote, local)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    else:
        used = "base64"
        _pull_b64(client, vm, remote, local)
    status("pulled %s -> %s (%s)" % (remote, local, used))
    return 0


def cmd_sync(client, vm, args):
    localdir = args.localdir or vm.get("staging_local")
    remotedir = args.remotedir or vm.get("staging_remote")
    if not localdir:
        die("no localdir given and no staging_local in config")
    if not remotedir:
        die("no remotedir given and no staging_remote in config")
    if not os.path.isdir(localdir):
        die("local dir not found: %s" % localdir)

    files = []
    for root, _dirs, names in os.walk(localdir):
        for n in names:
            files.append(os.path.join(root, n))

    exec_command(client, vm, "mkdir -p %s" % shlex.quote(remotedir), timeout=30)
    sftp = _sftp_or_none(client)
    count = 0
    made = set()
    for lf in files:
        rel = os.path.relpath(lf, localdir).replace(os.sep, "/")
        rf = posixpath.join(remotedir, rel)
        parent = posixpath.dirname(rf)
        if parent and parent not in made:
            exec_command(client, vm, "mkdir -p %s" % shlex.quote(parent), timeout=30)
            made.add(parent)
        ok = False
        if sftp is not None:
            try:
                sftp.put(lf, rf)
                ok = True
            except Exception:
                ok = False
        if not ok:
            _push_b64(client, vm, lf, rf)
        count += 1
    if sftp is not None:
        try:
            sftp.close()
        except Exception:
            pass
    status("synced %d file(s) %s -> %s" % (count, localdir, remotedir))
    return 0


# Compile recipes per source extension. .sh/.py execute directly.
def _build_cmd(remote_src, ext, tmp_bin):
    if ext == ".c":
        return "gcc -O0 -o %s %s" % (shlex.quote(tmp_bin), shlex.quote(remote_src))
    if ext in (".cc", ".cpp", ".cxx"):
        return "g++ -O0 -o %s %s" % (shlex.quote(tmp_bin), shlex.quote(remote_src))
    return None  # interpreted / no compile


def cmd_build_run(client, vm, args):
    local = args.source
    if not os.path.isfile(local):
        die("local source not found: %s" % local)
    ext = os.path.splitext(local)[1].lower()
    base = os.path.basename(local)
    stem = os.path.splitext(base)[0] or "a"

    # --dir: build into a caller-chosen remote dir and leave artifacts there.
    # Otherwise use a unique temp dir so concurrent agents don't collide.
    user_dir = args.dir
    if user_dir:
        workdir = user_dir
        rc, out, err = exec_command(client, vm, "mkdir -p %s" % shlex.quote(workdir), timeout=30)
        if rc != 0:
            die("could not create --dir %s: %s" % (workdir, err.decode(errors="replace")))
    else:
        rc, out, err = exec_command(client, vm, "mktemp -d /tmp/vmbuild.XXXXXX", timeout=30)
        if rc != 0:
            die("mktemp failed: %s" % err.decode(errors="replace"))
        workdir = out.decode().strip()
    # Keep artifacts when the caller named the dir (that's the point) or asked with --keep.
    keep = args.keep or bool(user_dir)
    remote_src = posixpath.join(workdir, base)

    # upload source (sftp with base64 fallback)
    sftp = _sftp_or_none(client)
    pushed = False
    if sftp is not None:
        try:
            sftp.put(local, remote_src)
            pushed = True
        except Exception:
            pushed = False
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    if not pushed:
        _push_b64(client, vm, local, remote_src)

    # Interpreted scripts authored on Windows carry CRLF, which breaks bash
    # (`exit 0\r: numeric argument required`) and shebang lines. Normalize to LF.
    if ext in (".sh", ".py"):
        exec_command(client, vm, "sed -i 's/\\r$//' " + shlex.quote(remote_src), timeout=30)

    tmp_bin = posixpath.join(workdir, stem)
    build = _build_cmd(remote_src, ext, tmp_bin)
    if build is not None:
        rc, out, err = exec_command(client, vm, build, timeout=args.timeout)
        if rc != 0:
            sys.stderr.buffer.write(err)
            sys.stderr.buffer.flush()
            _cleanup(client, vm, workdir, keep)
            die("compile failed (rc=%d)" % rc, code=rc if rc else 1)
        target = tmp_bin
    else:
        # interpreted: run the source itself
        if ext == ".py":
            target = "python3 " + shlex.quote(remote_src)
        elif ext == ".sh":
            target = "bash " + shlex.quote(remote_src)
        else:
            _cleanup(client, vm, workdir, keep)
            die("don't know how to build/run %s" % ext)

    arg_str = " ".join(shlex.quote(a) for a in (args.args or []))
    if build is not None:
        run_cmd = "%s %s" % (shlex.quote(target), arg_str)
    else:
        run_cmd = "%s %s" % (target, arg_str)
    run_cmd = run_cmd.strip()

    try:
        rc, out, err = exec_command(
            client, vm, run_cmd, as_user=args.as_user, timeout=args.timeout)
    except TimeoutError as e:
        _cleanup(client, vm, workdir, keep)
        die(str(e), EXIT_TIMEOUT)

    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    if err:
        sys.stderr.buffer.write(err)
        sys.stderr.buffer.flush()
    _cleanup(client, vm, workdir, keep)
    return rc


def _cleanup(client, vm, workdir, keep):
    if keep:
        status("kept build dir: %s" % workdir)
        return
    try:
        exec_command(client, vm, "rm -rf %s" % shlex.quote(workdir), timeout=30)
    except Exception:
        pass


# --- snap / verify / waitfile ------------------------------------------------
# One-liner remote snapshot: "inode mtime size sha256" or "MISSING" if absent.
_SNAP_SH = (
    'p={p}; if [ -e "$p" ]; then '
    'i=$(stat -c %i "$p"); m=$(stat -c %Y "$p"); s=$(stat -c %s "$p"); '
    'if [ -f "$p" ]; then h=$(sha256sum "$p" | cut -d" " -f1); else h=-; fi; '
    'echo "$i $m $s $h"; else echo MISSING; fi'
)


def _snapshot(client, vm, path):
    sh = _SNAP_SH.format(p=shlex.quote(path))
    rc, out, err = exec_command(client, vm, sh, timeout=60)
    return out.decode(errors="replace").strip()


def cmd_snap(client, vm, args):
    line = _snapshot(client, vm, args.path)
    print(line)
    return 0


def cmd_verify(client, vm, args):
    baseline = (args.baseline or "").strip()
    current = _snapshot(client, vm, args.path)

    base_missing = (baseline == "" or baseline == "MISSING")
    cur_missing = (current == "MISSING")

    if cur_missing:
        result = "MISSING" if base_missing else "MODIFIED"
    elif base_missing:
        result = "CREATED"
    elif current == baseline:
        result = "UNCHANGED"
    else:
        result = "MODIFIED"

    line = result
    if args.token is not None:
        found = False
        if not cur_missing:
            rc, out, err = exec_command(
                client, vm,
                "grep -qF -- %s %s" % (shlex.quote(args.token), shlex.quote(args.path)),
                timeout=60)
            found = (rc == 0)
        line += " TOKEN=%s" % ("present" if found else "absent")
    print(line)
    return 0


def cmd_waitfile(client, vm, args):
    path = args.path
    timeout = args.timeout
    baseline = _snapshot(client, vm, path)
    deadline = time.time() + timeout
    poll = 1.0
    while True:
        current = _snapshot(client, vm, path)
        if current != baseline and current != "MISSING":
            status("changed: %s" % path)
            return 0
        if baseline == "MISSING" and current != "MISSING":
            status("appeared: %s" % path)
            return 0
        if time.time() >= deadline:
            die("waitfile timed out after %ss: %s" % (timeout, path), EXIT_TIMEOUT)
        time.sleep(poll)


# --- Host / vmrun ------------------------------------------------------------
def vmrun_path(cfg):
    p = cfg.get("vmrun")
    if not p:
        die("no 'vmrun' path in config")
    return p


def run_vmrun(cfg, vmrun_args, timeout=120):
    """Invoke vmrun.exe with the given args; return (rc, stdout, stderr)."""
    exe = vmrun_path(cfg)
    if not os.path.isfile(exe):
        die("vmrun.exe not found: %s" % exe)
    cmd = [exe] + vmrun_args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die("vmrun timed out: %s" % " ".join(vmrun_args), EXIT_TIMEOUT)
    except FileNotFoundError:
        die("cannot execute vmrun: %s" % exe)
    return p.returncode, p.stdout, p.stderr


def vmx_path(vm):
    p = vm.get("vmx")
    if not p:
        die("VM has no 'vmx' path configured")
    return p


def cmd_vm_snapshot(cfg, vm, args):
    snap = args.snap or vm.get("snapshot")
    if not snap:
        die("no snapshot name given and no 'snapshot' in config")
    rc, out, err = run_vmrun(cfg, ["snapshot", vmx_path(vm), snap])
    if rc != 0:
        die("vmrun snapshot failed: %s" % (err or out).strip(), rc or 1)
    status("snapshot taken: %s" % snap)
    return 0


def cmd_vm_revert(cfg, vm, args):
    snap = args.snap or vm.get("snapshot")
    if not snap:
        die("no snapshot name given and no 'snapshot' in config")
    rc, out, err = run_vmrun(cfg, ["revertToSnapshot", vmx_path(vm), snap])
    if rc != 0:
        die("vmrun revert failed: %s" % (err or out).strip(), rc or 1)
    status("reverted to snapshot: %s" % snap)
    return 0


def cmd_vm_start(cfg, vm, args):
    rc, out, err = run_vmrun(cfg, ["start", vmx_path(vm), "nogui"])
    if rc != 0:
        die("vmrun start failed: %s" % (err or out).strip(), rc or 1)
    status("started")
    return 0


def cmd_vm_stop(cfg, vm, args):
    rc, out, err = run_vmrun(cfg, ["stop", vmx_path(vm), "soft"])
    if rc != 0:
        # try hard stop as fallback
        rc, out, err = run_vmrun(cfg, ["stop", vmx_path(vm), "hard"])
        if rc != 0:
            die("vmrun stop failed: %s" % (err or out).strip(), rc or 1)
    status("stopped")
    return 0


def cmd_vm_list(cfg, vm, args):
    rc, out, err = run_vmrun(cfg, ["list"])
    if rc != 0:
        die("vmrun list failed: %s" % (err or out).strip(), rc or 1)
    sys.stdout.write(out)
    return 0


def cmd_vm_snapshots(cfg, vm, args):
    rc, out, err = run_vmrun(cfg, ["listSnapshots", vmx_path(vm)])
    if rc != 0:
        die("vmrun listSnapshots failed: %s" % (err or out).strip(), rc or 1)
    sys.stdout.write(out)
    return 0


def _get_ip(cfg, vm, retries=30, delay=2):
    """getGuestIPAddress with a retry loop (Tools not up immediately after boot)."""
    last = ""
    for _ in range(retries):
        rc, out, err = run_vmrun(cfg, ["getGuestIPAddress", vmx_path(vm), "-wait"],
                                 timeout=60)
        ip = out.strip()
        if rc == 0 and ip and ip[0].isdigit():
            return ip
        last = (err or out).strip()
        time.sleep(delay)
    return None if not last else None


def cmd_vm_ip(cfg, vm, vm_name, args, config_path):
    ip = _get_ip(cfg, vm)
    if not ip:
        die("could not determine guest IP (VMware Tools up?)", EXIT_ENV)
    print(ip)
    if args.save:
        cfg["vms"][vm_name]["host"] = ip
        atomic_write_json(config_path, cfg)
        status("saved host=%s to config" % ip)
    return 0


def _wait_for_ssh(vm, host, timeout=180):
    """Poll SSH until it accepts a connection or timeout."""
    user = vm.get("default_user")
    password = user_password(vm, user)
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=host, username=user, password=password,
                           timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
                           auth_timeout=CONNECT_TIMEOUT, look_for_keys=False,
                           allow_agent=False)
            client.close()
            return True
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            time.sleep(3)
    return False


def cmd_vm_reset(cfg, vm, vm_name, args, config_path):
    snap = vm.get("snapshot")
    if not snap:
        die("no 'snapshot' configured for reset")
    rc, out, err = run_vmrun(cfg, ["revertToSnapshot", vmx_path(vm), snap])
    if rc != 0:
        die("revert failed: %s" % (err or out).strip(), rc or 1)
    status("reverted to %s" % snap)
    rc, out, err = run_vmrun(cfg, ["start", vmx_path(vm), "nogui"])
    if rc != 0:
        die("start failed: %s" % (err or out).strip(), rc or 1)
    status("powered on; discovering IP...")
    ip = _get_ip(cfg, vm)
    if not ip:
        die("VM booted but no guest IP (Tools?)", EXIT_ENV)
    cfg["vms"][vm_name]["host"] = ip
    atomic_write_json(config_path, cfg)
    status("IP=%s (saved); waiting for SSH..." % ip)
    if not _wait_for_ssh(vm, ip):
        die("VM up at %s but SSH never came up" % ip, EXIT_ENV)
    status("reset complete: %s reachable at %s" % (vm_name, ip))
    return 0


def cmd_vm_doctor(cfg, vm, vm_name, args, config_path):
    """Per-check PASS/FAIL health report."""
    checks = []

    # config already parsed to get here
    checks.append(("config parses", True, config_path))

    exe = cfg.get("vmrun")
    ok_exe = bool(exe) and os.path.isfile(exe)
    checks.append(("vmrun.exe exists", ok_exe, exe or "(unset)"))

    vmx = vm.get("vmx")
    ok_vmx = bool(vmx) and os.path.isfile(vmx)
    checks.append(("vmx exists", ok_vmx, vmx or "(unset)"))

    # SSH connect
    ssh_ok = False
    ssh_detail = ""
    client = None
    try:
        host = vm.get("host")
        user = vm.get("default_user")
        password = user_password(vm, user) if user else ""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, username=user, password=password,
                       timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
                       auth_timeout=CONNECT_TIMEOUT, look_for_keys=False,
                       allow_agent=False)
        ssh_ok = True
        ssh_detail = "%s@%s" % (user, host)
    except Exception as e:
        ssh_detail = str(e)
    checks.append(("ssh connects", ssh_ok, ssh_detail))

    # sudo works for each configured user
    users = vm.get("users") or {}
    if ssh_ok:
        for uname in sorted(users):
            try:
                rc, out, err = exec_command(client, vm, "id -un", as_user=uname,
                                            timeout=30)
                got = out.decode(errors="replace").strip()
                ok = (rc == 0 and got == uname)
                detail = got if got else (err.decode(errors="replace").strip() or "rc=%d" % rc)
            except Exception as e:
                ok = False
                detail = str(e)
            checks.append(("sudo as %s" % uname, ok, detail))
    else:
        for uname in sorted(users):
            checks.append(("sudo as %s" % uname, False, "ssh down"))

    if client is not None:
        try:
            client.close()
        except Exception:
            pass

    all_ok = True
    for name, ok, detail in checks:
        tag = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print("[%s] %-18s %s" % (tag, name, detail))
    return 0 if all_ok else EXIT_ENV


# --- Optional WSL mount ------------------------------------------------------
def cmd_mount(cfg, vm, args):
    distro = vm.get("wsl_distro")
    if not distro:
        die("no wsl_distro configured for this VM")
    host = vm.get("host")
    user = vm.get("default_user")
    password = user_password(vm, user)
    remote = vm.get("staging_remote")
    local = vm.get("staging_local")
    if not remote or not local:
        die("staging_remote and staging_local must be set for mount")
    mnt = "/mnt/vmstaging_%s" % vm.get("host", "vm").replace(".", "_")
    sshfs = ("mkdir -p %s && echo %s | sshfs -o password_stdin,"
             "StrictHostKeyChecking=no,reconnect %s@%s:%s %s" % (
                 shlex.quote(mnt), shlex.quote(password),
                 shlex.quote(user), shlex.quote(host),
                 shlex.quote(remote), shlex.quote(mnt)))
    try:
        p = subprocess.run(["wsl", "-d", distro, "bash", "-c", sshfs],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        die("wsl mount failed: %s" % e)
    if p.returncode != 0:
        die("sshfs mount failed: %s" % (p.stderr or p.stdout).strip())
    status("mounted %s:%s at %s (wsl %s)" % (host, remote, mnt, distro))
    return 0


def cmd_umount(cfg, vm, args):
    distro = vm.get("wsl_distro")
    if not distro:
        die("no wsl_distro configured for this VM")
    mnt = "/mnt/vmstaging_%s" % vm.get("host", "vm").replace(".", "_")
    try:
        p = subprocess.run(["wsl", "-d", distro, "bash", "-c",
                            "fusermount -u %s" % shlex.quote(mnt)],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        die("wsl umount failed: %s" % e)
    if p.returncode != 0:
        die("umount failed: %s" % (p.stderr or p.stdout).strip())
    status("unmounted %s" % mnt)
    return 0


# --- Argument parsing --------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="vm.py",
        description="Generic VM automation: guest control over SSH + host control via vmrun.")
    p.add_argument("--vm", help="named VM from config (default: config default_vm)")
    p.add_argument("--config", help="config path (default: vmconfig.json next to vm.py, "
                                     "or $VM_CONFIG)")
    sub = p.add_subparsers(dest="verb", required=True)

    # run
    s = sub.add_parser("run", help="run a command on the guest over SSH")
    s.add_argument("command", help="command string to run")
    s.add_argument("--as", dest="as_user", metavar="USER", help="run as another user via sudo")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="command timeout (s)")

    # push
    s = sub.add_parser("push", help="upload file(s) (SFTP, base64 fallback)")
    s.add_argument("paths", nargs="+", metavar="SRC... [DEST]",
                   help="cp-style: one file uses a default remote; SRC DEST sets a remote path; "
                        "SRC... DESTDIR pushes many files into a remote directory")

    # pull
    s = sub.add_parser("pull", help="download a file (SFTP, base64 fallback)")
    s.add_argument("remote")
    s.add_argument("local", nargs="?", help="local path (default: ./basename)")

    # sync
    s = sub.add_parser("sync", help="bulk push a directory (defaults from config staging)")
    s.add_argument("localdir", nargs="?")
    s.add_argument("remotedir", nargs="?")

    # build-run
    s = sub.add_parser("build-run", help="push, compile on VM, run, stream output+rc")
    s.add_argument("source", help="local source file (.c/.cpp compiled; .sh/.py run directly)")
    s.add_argument("--as", dest="as_user", metavar="USER", help="run as another user via sudo")
    s.add_argument("--args", nargs=argparse.REMAINDER, default=[],
                   help="arguments passed to the program (must be last)")
    s.add_argument("--keep", action="store_true", help="keep the temp build dir on the VM")
    s.add_argument("--dir", metavar="REMOTE",
                   help="build in this remote dir (created if needed) and leave artifacts there, "
                        "instead of a temp dir")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="build/run timeout (s)")

    # snap
    s = sub.add_parser("snap", help="print one-line 'inode mtime size sha256' snapshot")
    s.add_argument("path")

    # verify
    s = sub.add_parser("verify", help="compare path to a baseline snap-line")
    s.add_argument("path")
    s.add_argument("--baseline", required=True, help="the snap-line to compare against")
    s.add_argument("--token", help="also report whether this token appears in file content")

    # waitfile
    s = sub.add_parser("waitfile", help="block until a file exists/changes")
    s.add_argument("path")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="max wait (s)")

    # vm (host control)
    s = sub.add_parser("vm", help="host-side VMware control via vmrun")
    vsub = s.add_subparsers(dest="vmverb", required=True)
    x = vsub.add_parser("snapshot", help="take a snapshot (default: config snapshot)")
    x.add_argument("snap", nargs="?")
    x = vsub.add_parser("revert", help="revert to a snapshot (default: config snapshot)")
    x.add_argument("snap", nargs="?")
    vsub.add_parser("reset", help="revert to clean snapshot -> boot -> wait SSH -> refresh IP")
    vsub.add_parser("start", help="power on")
    vsub.add_parser("stop", help="power off")
    vsub.add_parser("list", help="list running VMs")
    vsub.add_parser("snapshots", help="list this VM's snapshots")
    x = vsub.add_parser("ip", help="discover guest IP")
    x.add_argument("--save", action="store_true", help="write IP back to config (atomic)")
    vsub.add_parser("doctor", help="validate config/vmrun/vmx/SSH/sudo with PASS/FAIL report")

    # mount / umount (optional WSL)
    sub.add_parser("mount", help="(optional) sshfs live-bind staging dir via WSL")
    sub.add_parser("umount", help="(optional) unmount the WSL sshfs bind")

    return p


def resolve_config_path(args):
    if args.config:
        return args.config
    env = os.environ.get("VM_CONFIG")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "vmconfig.json")


# Verbs that need an SSH client vs those that are host-only.
GUEST_VERBS = {"run", "push", "pull", "sync", "build-run", "snap", "verify", "waitfile"}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = resolve_config_path(args)
    cfg = load_config(config_path)
    vm_name, vm = resolve_vm(cfg, args.vm)

    verb = args.verb

    # Host-side vmrun verbs
    if verb == "vm":
        vv = args.vmverb
        if vv == "snapshot":
            return cmd_vm_snapshot(cfg, vm, args)
        if vv == "revert":
            return cmd_vm_revert(cfg, vm, args)
        if vv == "reset":
            return cmd_vm_reset(cfg, vm, vm_name, args, config_path)
        if vv == "start":
            return cmd_vm_start(cfg, vm, args)
        if vv == "stop":
            return cmd_vm_stop(cfg, vm, args)
        if vv == "list":
            return cmd_vm_list(cfg, vm, args)
        if vv == "snapshots":
            return cmd_vm_snapshots(cfg, vm, args)
        if vv == "ip":
            return cmd_vm_ip(cfg, vm, vm_name, args, config_path)
        if vv == "doctor":
            return cmd_vm_doctor(cfg, vm, vm_name, args, config_path)
        die("unknown vm subcommand: %s" % vv)

    if verb == "mount":
        return cmd_mount(cfg, vm, args)
    if verb == "umount":
        return cmd_umount(cfg, vm, args)

    # Guest-side SSH verbs
    if verb in GUEST_VERBS:
        client = ssh_connect(vm)
        try:
            if verb == "run":
                return cmd_run(client, vm, args)
            if verb == "push":
                return cmd_push(client, vm, args)
            if verb == "pull":
                return cmd_pull(client, vm, args)
            if verb == "sync":
                return cmd_sync(client, vm, args)
            if verb == "build-run":
                return cmd_build_run(client, vm, args)
            if verb == "snap":
                return cmd_snap(client, vm, args)
            if verb == "verify":
                return cmd_verify(client, vm, args)
            if verb == "waitfile":
                return cmd_waitfile(client, vm, args)
        finally:
            try:
                client.close()
            except Exception:
                pass

    die("unknown verb: %s" % verb)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(130)
