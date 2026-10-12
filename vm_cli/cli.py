#!/usr/bin/env python3
"""VM automation CLI: guest commands over SSH and host control over VMware.

Guest commands use SSH; VMware power and snapshot commands use vmrun.

  Guest (SSH/paramiko):  run, push, pull, sync, build-run, snap, verify, waitfile, port
  Host  (vmrun.exe):     vm snapshot|revert|reset|start|stop|list|snapshots|ip|doctor
  Optional (WSL sshfs):  mount, umount

Usage:  vm [--vm NAME] [--config PATH] <verb> ...

Exit codes:  remote rc passes through for `run`/`build-run`;
             124 = timeout, 125 = can't connect / config error / bad usage, 0 = success.
"""

import argparse
import base64
import glob
import hashlib
import html
import ipaddress
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

try:
    from vm_cli import docs
except ImportError:  # running this file directly, not as `python -m vm_cli.cli`
    import docs

try:
    import paramiko
except ImportError:
    sys.stderr.write("error: paramiko is required (pip install paramiko)\n")
    sys.exit(125)

# --- Reserved exit codes -----------------------------------------------------
EXIT_TIMEOUT = 124
EXIT_ENV = 125  # can't connect / config error / bad usage

CONNECT_TIMEOUT = 10  # paramiko TCP/auth timeout (seconds)
DEFAULT_TIMEOUT = 120  # default remote command timeout (seconds)

# Directory the config lives in; provision/ and other project files anchor here.
# Set from the resolved config path in main(); defaults to the working directory so
# the installed `vm` command finds vmconfig.json / provision/ in the user's project.
PROJECT_DIR = os.getcwd()


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
        with open(path, "r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        die(
            "config file not found: %s\n"
            "first-time setup: run `vm-init` here to create vmconfig.json, "
            "then run: vm doctor" % path
        )
    except json.JSONDecodeError as error:
        die("config file is not valid JSON: %s" % error)
    except (OSError, UnicodeError) as error:
        die("cannot read config %s: %s" % (path, error))
    if not isinstance(config, dict):
        die("config must be a JSON object")
    vms = config.get("vms", {})
    if not isinstance(vms, dict) or any(
        not isinstance(vm, dict) for vm in vms.values()
    ):
        die("config 'vms' must map VM names to objects")
    if config.get("vmrun") is not None and not isinstance(config["vmrun"], str):
        die("config 'vmrun' must be a path string")
    for name, vm in vms.items():
        for key in ("os", "host", "default_user", "vmx", "encryption_password"):
            if vm.get(key) is not None and not isinstance(vm[key], str):
                die("vms.%s.%s must be a string" % (name, key))
        if vm.get("os") and guest_os(vm) not in {"linux", "windows"}:
            die("vms.%s.os must be linux or windows" % name)
        users = vm.get("users", {})
        if not isinstance(users, dict) or any(
            not isinstance(user, dict) for user in users.values()
        ):
            die("vms.%s.users must map usernames to objects" % name)
    return config


def resolve_vm(cfg, name):
    """Return (vm_name, vm_block) for the selected VM."""
    vms = cfg.get("vms") or {}
    if not vms:
        die("no VMs defined in config")
    if name is None:
        name = cfg.get("default_vm")
    if name is None and len(vms) == 1:
        name = next(iter(vms))
    if name is None:
        die(
            "choose a VM with --vm NAME (known: %s), or set default_vm"
            % ", ".join(sorted(vms))
        )
    if not isinstance(name, str):
        die("default_vm must be a VM name")
    if name not in vms:
        die("unknown VM %r (known: %s)" % (name, ", ".join(sorted(vms))))
    return name, vms[name]


def atomic_write_json(path, data):
    """Write JSON atomically: temp file in same dir + os.replace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    file_descriptor, temporary_path = tempfile.mkstemp(
        dir=directory, prefix=".vmconfig.", suffix=".tmp"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def user_password(vm, user):
    users = vm.get("users") or {}
    if not isinstance(users, dict) or user not in users:
        raise ValueError("user %r not configured for this VM" % user)
    account = users[user]
    if not isinstance(account, dict) or not isinstance(
        account.get("password", ""), str
    ):
        raise ValueError("users.%s must be an object with a string password" % user)
    return account.get("password", "")


def guest_os(vm):
    """'linux' (default) or 'windows' - selects OS-specific behavior."""
    return (vm.get("os") or "linux").strip().lower()


def tools_remote(vm):
    """Guest dir where staged tools live and which `run` prepends to PATH.

    Config key `tools_remote` overrides the default (<home>/tools)."""
    configured_path = vm.get("tools_remote")
    if configured_path:
        return configured_path
    user = vm.get("default_user") or "user"
    if guest_os(vm) == "windows":
        return "C:/Users/%s/tools" % user
    return "/root/tools" if user == "root" else "/home/%s/tools" % user


def _wrap_tools_path(vm, cmd):
    """Prepend the staged tools dir to PATH for a `run` command (OS-aware)."""
    tools = tools_remote(vm)
    if guest_os(vm) == "windows":
        return 'set "PATH=%s;%%PATH%%" & %s' % (tools.replace("/", "\\"), cmd)
    return 'export PATH=%s:"$PATH"; %s' % (shlex.quote(tools), cmd)


# --- SSH ---------------------------------------------------------------------
def _ssh_open(vm, timeout=CONNECT_TIMEOUT, user=None):
    """Attempt one paramiko connection; return client or raise."""
    host = vm.get("host")
    if not host:
        raise ValueError("VM has no host/IP configured (run: vm ip --save)")
    user = user or vm.get("default_user")
    if not user:
        raise ValueError("VM has no default_user configured")
    port = vm.get("ssh_port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("ssh_port must be an integer in 1-65535")
    password = user_password(vm, user)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        client.get_transport().set_keepalive(30)
    except Exception:
        client.close()
        raise
    return client


def ssh_connect(cfg, vm):
    """Connect once, then recover a stale VMware IP after a network failure.

    Authentication and config failures never trigger guest setup. A discovered
    address is used in memory; `vm ip --save` explicitly persists it.
    """
    user = vm.get("default_user")
    host = vm.get("host")
    try:
        return _ssh_open(vm)
    except Exception as connection_error:
        error = connection_error
    if isinstance(error, paramiko.AuthenticationException):
        die(
            "SSH authentication failed for %s@%s; check users.%s.password in config"
            % (user, host, user)
        )
    if isinstance(error, (ValueError, paramiko.BadHostKeyException)):
        die(str(error))
    if not isinstance(error, (OSError, EOFError, paramiko.SSHException)):
        die("cannot connect to %s@%s: %s" % (user, host, error))

    # Tools can report a DHCP address that changed since init or a snapshot.
    # No -wait and one short attempt: a powered-off VM must still fail promptly.
    ip = None
    if cfg.get("vmrun") and os.path.isfile(cfg["vmrun"]) and vm.get("vmx"):
        ip = _get_ip(cfg, vm, retries=1, delay=0, timeout=3)
    if ip and ip != host:
        status(
            "configured IP %s is unreachable; retrying VMware IP %s "
            "(persist with: vm ip --save)" % (host, ip)
        )
        host = ip
        vm["host"] = ip
        try:
            return _ssh_open(vm)
        except paramiko.AuthenticationException:
            die(
                "SSH authentication failed for %s@%s; check users.%s.password in config"
                % (user, host, user)
            )
        except Exception as connection_error:
            error = connection_error

    # Only a refused TCP connection suggests SSH is absent. Timeouts, auth
    # failures and protocol errors are not evidence that Windows needs an install.
    refused = isinstance(error, ConnectionRefusedError) or (
        isinstance(error, paramiko.ssh_exception.NoValidConnectionsError)
        and all(
            isinstance(connection_error, ConnectionRefusedError)
            for connection_error in error.errors.values()
        )
    )
    if guest_os(vm) == "windows" and refused and ip and vm.get("ssh_port", 22) == 22:
        status("SSH not reachable; enabling OpenSSH in the Windows guest...")
        if bootstrap_windows_ssh(cfg, vm):
            try:
                return _ssh_open(vm)
            except Exception as bootstrap_error:
                die(
                    "Windows guest bootstrap ran but SSH still refused: %s"
                    % bootstrap_error,
                    EXIT_ENV,
                )
    die(
        "cannot connect to %s@%s: %s\n"
        "check that the VM is powered on; run: vm ip --save, then vm doctor"
        % (user, host, error),
        EXIT_ENV,
    )


def _read_all(chan, timeout, stream=False):
    """Drain both channels fairly; streaming forwards bytes without retaining them."""
    chan.settimeout(1.0)
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout

    def check_timeout():
        if time.monotonic() >= deadline:
            chan.close()
            raise TimeoutError("command timed out after %ss" % timeout)

    while True:
        if chan.recv_ready():
            check_timeout()
            chunk = chan.recv(65536)
            if stream:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            else:
                stdout += chunk
        if chan.recv_stderr_ready():
            check_timeout()
            chunk = chan.recv_stderr(65536)
            if stream:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
            else:
                stderr += chunk
        if (
            chan.exit_status_ready()
            and not chan.recv_ready()
            and not chan.recv_stderr_ready()
        ):
            break
        check_timeout()
        if not chan.recv_ready() and not chan.recv_stderr_ready():
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    # drain any remainder
    while chan.recv_ready():
        stdout += chan.recv(65536)
    while chan.recv_stderr_ready():
        stderr += chan.recv_stderr(65536)
    return_code = chan.recv_exit_status()
    return return_code, bytes(stdout), bytes(stderr)


def exec_command(
    client,
    vm,
    cmd,
    as_user=None,
    timeout=DEFAULT_TIMEOUT,
    stdin_data=None,
    stream=False,
):
    """Run a command on the guest. --as USER wraps it in piped `sudo -S`.

    Returns (rc, stdout_bytes, stderr_bytes). With stream=True, output is forwarded
    immediately and returned byte strings are empty. Raises TimeoutError on timeout.
    """
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise paramiko.SSHException("SSH connection closed before command execution")
    deadline = time.monotonic() + timeout
    channel = transport.open_session(timeout=timeout)
    try:
        channel.settimeout(timeout)
        input_data = stdin_data
        if as_user is not None:
            user_password(vm, as_user)  # validate the requested identity
            password = user_password(vm, vm.get("default_user"))
            # sudo authenticates the invoking SSH user, not the target user.
            cmd = "sudo -S -p '' -u %s -- bash -c %s" % (
                shlex.quote(as_user),
                shlex.quote(cmd),
            )
            input_data = (password + "\n").encode() + (stdin_data or b"")
        channel.exec_command(cmd)
        if input_data is not None:
            try:
                channel.sendall(input_data)
            except OSError:
                # A command may exit before consuming stdin; collect its rc.
                pass
        channel.shutdown_write()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("command timed out after %ss" % timeout)
        if stream:
            return _read_all(channel, remaining, stream=True)
        return _read_all(channel, remaining)
    finally:
        channel.close()


# --- Guest verbs -------------------------------------------------------------
def cmd_run(client, vm, args):
    try:
        return_code, stdout, stderr = exec_command(
            client,
            vm,
            _wrap_tools_path(vm, args.command),
            as_user=args.as_user,
            timeout=args.timeout,
            stream=True,
        )
    except TimeoutError as error:
        die(str(error), EXIT_TIMEOUT)
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    if stderr:
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
    return return_code


def _sftp_or_none(client):
    try:
        return client.open_sftp()
    except Exception:
        return None


def _win_ps_b64_decode_cmd(remote):
    """Build a `powershell -EncodedCommand ...` line that reads base64 from stdin,
    creates the destination's parent dir, and writes the decoded bytes to `remote`.

    The script is passed as -EncodedCommand (UTF-16LE base64) so the guest's
    default cmd.exe shell never has to quote it -- the command line is pure base64.
    The file's own base64 is streamed on stdin, so large files don't hit the
    command-line length limit."""
    destination = remote.replace("/", "\\").replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$dst='%s';"
        "$dir=Split-Path -Parent $dst;"
        # Guard on existence: New-Item on an already-present dir (e.g. a drive
        # root like C:\) throws "path is not of a legal form", not a no-op.
        "if($dir -and -not(Test-Path -LiteralPath $dir)){"
        "New-Item -ItemType Directory -Force -Path $dir|Out-Null};"
        "$b=[Console]::In.ReadToEnd();"
        "[IO.File]::WriteAllBytes($dst,[Convert]::FromBase64String($b))"
    ) % destination
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode()
    return "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encoded_script


def _push_b64(client, vm, local, remote):
    """Fallback upload: base64 the local file, decode over exec on the guest.

    Linux decodes with `base64 -d`; Windows (cmd.exe shell, no `base64`) decodes
    with an inline PowerShell one-liner. Both stream the base64 over stdin."""
    with open(local, "rb") as file:
        data = file.read()
    encoded_data = base64.b64encode(data).decode()

    if guest_os(vm) == "windows":
        return_code, stdout, stderr = exec_command(
            client,
            vm,
            _win_ps_b64_decode_cmd(remote),
            timeout=DEFAULT_TIMEOUT,
            stdin_data=encoded_data.encode(),
        )
        if return_code != 0:
            die(
                "base64 push failed on Windows guest: %s"
                % stderr.decode(errors="replace")
            )
        return

    # Stream straight to the destination; a shared /tmp file races other pushes.
    parent = posixpath.dirname(remote)
    return_code, stdout, stderr = exec_command(
        client,
        vm,
        "mkdir -p %s && base64 -d > %s"
        % (shlex.quote(parent or "."), shlex.quote(remote)),
        timeout=DEFAULT_TIMEOUT,
        stdin_data=encoded_data.encode(),
    )
    if return_code != 0:
        die("base64 push decode failed: %s" % stderr.decode(errors="replace"))


def _ensure_remote_dir(client, vm, sftp, remote_dir):
    """Create a remote directory (and parents). OS-aware: `mkdir -p` on Linux,
    recursive SFTP mkdir on Windows (whose default cmd shell has no `mkdir -p`)."""
    if not remote_dir:
        return
    if guest_os(vm) == "windows":
        if sftp is None:
            return
        parts = remote_dir.replace("\\", "/").split("/")
        current_path = ""
        for part in parts:
            if not part:
                continue
            current_path = part if not current_path else current_path + "/" + part
            try:
                sftp.mkdir(current_path)
            except IOError:
                pass  # already exists
    else:
        return_code, stdout, stderr = exec_command(
            client, vm, "mkdir -p %s" % shlex.quote(remote_dir), timeout=30
        )
        if return_code != 0:
            die(
                "could not create remote dir %s: %s"
                % (remote_dir, stderr.decode(errors="replace").strip())
            )


def _remote_is_dir(client, vm, sftp, path):
    """True if `path` already exists as a directory on the guest.

    Used to give `push FILE DEST` cp-style semantics: a DEST that is an existing
    directory receives FILE by its basename, rather than being overwritten as a
    literal file path. Prefers a cheap SFTP stat; falls back to a shell test."""
    if not path:
        return False
    if sftp is not None:
        try:
            return stat.S_ISDIR(sftp.stat(path).st_mode)
        except IOError:
            return False
    if guest_os(vm) == "windows":
        script = (
            "if(Test-Path -LiteralPath '%s' -PathType Container){exit 0}" "else{exit 1}"
        ) % path.replace("/", "\\").replace("'", "''")
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode()
        command = (
            "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encoded_script
        )
    else:
        command = "test -d %s" % shlex.quote(path)
    return_code, _out, _err = exec_command(client, vm, command, timeout=30)
    return return_code == 0


def _push_file(client, vm, local, remote, sftp):
    """Upload one file; SFTP if available, base64-over-exec fallback. Returns method used."""
    if sftp is not None:
        try:
            _ensure_remote_dir(client, vm, sftp, posixpath.dirname(remote))
            sftp.put(local, remote)
            return "sftp"
        except Exception:
            pass
    _push_b64(client, vm, local, remote)
    return "base64"


_GLOB_MAGIC = set("*?[")


def _has_glob(s):
    return any(character in s for character in _GLOB_MAGIC)


def _rel_component(path):
    """Remote-relative path to preserve for a local source typed on the CLI.

    Keeps a clean relative path so `docs/a.txt` -> `docs/a.txt` (the `docs/`
    folder is recreated remotely). Falls back to the basename for absolute,
    drive-qualified, or `..`-containing paths, which must not leak a host-local
    layout (or escape) into the destination root."""
    normalized_path = path.replace("\\", "/").rstrip("/")
    parts = [part for part in normalized_path.split("/") if part not in ("", ".")]
    # Detect a Windows drive (C:/...) explicitly: os.path.splitdrive only sees it
    # on Windows, but a drive-qualified path must flatten on any host.
    drive_like = (
        len(normalized_path) >= 2
        and normalized_path[1] == ":"
        and normalized_path[0].isalpha()
    )
    if (
        normalized_path.startswith("/")
        or drive_like
        or os.path.splitdrive(path)[0]
        or ".." in parts
    ):
        return parts[-1] if parts else os.path.basename(path)
    return "/".join(parts) if parts else os.path.basename(path)


def _strip_top_component(comp, top):
    """Drop the leading `top/` from a rel component (as produced by
    _expand_sources for a directory), so the directory's *contents* map to the
    destination root. `top` itself (the empty-dir marker) collapses to ""."""
    if comp == top:
        return ""
    prefix = top + "/"
    return comp[len(prefix) :] if comp.startswith(prefix) else comp


def _expand_sources(paths):
    """Resolve CLI source args (files, dirs, globs) to (local_file, rel_component)
    pairs. rel_component is the path under the destination root, preserving the
    typed structure. A directory yields one pair per contained file (plus a
    (None, dir) marker if empty, so the remote dir is still created). Globs are
    expanded here because Windows shells don't expand them for the CLI."""
    pairs = []
    for source_argument in paths:
        if _has_glob(source_argument):
            matches = sorted(glob.glob(source_argument, recursive=True))
            if not matches:
                die("no files match: %s" % source_argument)
        else:
            matches = [source_argument]
        for matched_path in matches:
            if os.path.isdir(matched_path):
                source_root = _rel_component(matched_path)
                found_files = False
                for root, _dirs, names in os.walk(matched_path):
                    for filename in sorted(names):
                        local_file = os.path.join(root, filename)
                        relative_path = os.path.relpath(
                            local_file, matched_path
                        ).replace(os.sep, "/")
                        pairs.append(
                            (local_file, posixpath.join(source_root, relative_path))
                        )
                        found_files = True
                if not found_files:
                    pairs.append((None, source_root))
            elif os.path.isfile(matched_path):
                pairs.append((matched_path, _rel_component(matched_path)))
            else:
                die("local path not found: %s" % matched_path)
    return pairs


def cmd_push(client, vm, args):
    """Upload files, directories, and globs, preserving each source's relative
    path under the destination (default: staging_remote).

      push report.txt              -> <staging>/report.txt
      push docs/*.txt              -> <staging>/docs/<each>.txt   (docs/ created)
      push docs                    -> <staging>/docs/...          (recursive)
      push a.txt b.txt REMOTEDIR   -> REMOTEDIR/a.txt, REMOTEDIR/b.txt
      push local.txt /remote/x.txt -> single-file rename to a literal remote path
    """
    paths = args.paths
    # Destination detection: with 2+ args, a trailing arg that exists neither as
    # a local file/dir nor as a glob match is an explicit remote destination.
    destination = getattr(args, "dest", None)
    sources = paths
    if destination is None and len(paths) >= 2:
        last_argument = paths[-1]
        matches_local_source = os.path.exists(last_argument) or (
            _has_glob(last_argument) and glob.glob(last_argument, recursive=True)
        )
        if not matches_local_source:
            destination = last_argument
            sources = paths[:-1]

    pairs = _expand_sources(sources)
    if not pairs:
        die("nothing to push")

    # A single directory source + explicit dest means dest *becomes* that
    # directory: its contents map directly under dest, rather than nesting the
    # source directory's own name beneath dest. Mirrors `rsync src/ dest`:
    #   push .../files c:\files  ->  c:\files\file1.txt, c:\files\dir\extra.txt
    single_directory = (
        destination is not None
        and len(sources) == 1
        and not _has_glob(sources[0])
        and os.path.isdir(sources[0])
    )
    if single_directory:
        source_root = _rel_component(sources[0])
        pairs = [
            (local_file, _strip_top_component(relative_path, source_root))
            for local_file, relative_path in pairs
        ]

    sftp = _sftp_or_none(client)
    try:
        # Single file + explicit dest: a literal remote path (cp-style rename),
        # unless dest is an existing guest directory -- then drop the file into
        # it by basename (cp FILE DIR/), rather than overwriting the directory.
        if (
            not single_directory
            and destination is not None
            and len(pairs) == 1
            and pairs[0][0] is not None
        ):
            local, remote = pairs[0][0], destination
            if _remote_is_dir(client, vm, sftp, destination):
                remote = posixpath.join(destination, os.path.basename(local))
            transfer_method = _push_file(client, vm, local, remote, sftp)
            status("pushed %s -> %s (%s)" % (local, remote, transfer_method))
            return 0

        root = destination or vm.get("staging_remote") or "/tmp"
        created_directories = set()
        count = 0
        last_remote = None
        for local, relative_path in pairs:
            remote = posixpath.join(root, relative_path)
            parent = posixpath.dirname(remote) if local is not None else remote
            if parent and parent not in created_directories:
                _ensure_remote_dir(client, vm, sftp, parent)
                created_directories.add(parent)
            if local is None:
                continue  # empty directory: just created above
            _push_file(client, vm, local, remote, sftp)
            count += 1
            last_remote = remote
        if count == 1:
            status("pushed %s -> %s" % (pairs[0][0], last_remote))
        else:
            status("pushed %d file(s) -> %s" % (count, root))
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
    return 0


def _pull_b64(client, vm, remote, local):
    if guest_os(vm) == "windows":
        path = remote.replace("/", "\\").replace("'", "''")
        script = "[Convert]::ToBase64String([IO.File]::ReadAllBytes('%s'))" % path
        return_code, stdout, stderr = _win_ps(
            client, vm, script, timeout=DEFAULT_TIMEOUT
        )
    else:
        return_code, stdout, stderr = exec_command(
            client, vm, "base64 %s" % shlex.quote(remote), timeout=DEFAULT_TIMEOUT
        )
        stderr = stderr.decode(errors="replace")
    if return_code != 0:
        die("base64 pull failed: %s" % stderr)
    data = base64.b64decode(stdout)
    with open(local, "wb") as file:
        file.write(data)


def cmd_pull(client, vm, args):
    remote = args.remote
    local = args.local or os.path.basename(remote)
    if local and (os.path.isdir(local)):
        local = os.path.join(local, os.path.basename(remote))
    sftp = _sftp_or_none(client)
    transfer_method = "sftp"
    if sftp is not None:
        try:
            sftp.get(remote, local)
        except Exception:
            transfer_method = "base64"
            _pull_b64(client, vm, remote, local)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    else:
        transfer_method = "base64"
        _pull_b64(client, vm, remote, local)
    status("pulled %s -> %s (%s)" % (remote, local, transfer_method))
    return 0


def cmd_sync(client, vm, args):
    local_directory = args.localdir or vm.get("staging_local")
    if local_directory and args.localdir is None and not os.path.isabs(local_directory):
        local_directory = os.path.join(PROJECT_DIR, local_directory)
    remote_directory = args.remotedir or vm.get("staging_remote")
    if not local_directory:
        die("no localdir given and no staging_local in config")
    if not remote_directory:
        die("no remotedir given and no staging_remote in config")
    if not os.path.isdir(local_directory):
        die("local dir not found: %s" % local_directory)

    files = []
    for root, _dirs, names in os.walk(local_directory):
        for filename in names:
            files.append(os.path.join(root, filename))

    sftp = _sftp_or_none(client)
    try:
        _ensure_remote_dir(client, vm, sftp, remote_directory)
        count = 0
        created_directories = {remote_directory}
        for local_file in files:
            relative_path = os.path.relpath(local_file, local_directory).replace(
                os.sep, "/"
            )
            remote_file = posixpath.join(remote_directory, relative_path)
            parent = posixpath.dirname(remote_file)
            if parent and parent not in created_directories:
                _ensure_remote_dir(client, vm, sftp, parent)
                created_directories.add(parent)
            if sftp is not None:
                try:
                    sftp.put(local_file, remote_file)
                    count += 1
                    continue
                except OSError:
                    pass
            _push_b64(client, vm, local_file, remote_file)
            count += 1
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
    status("synced %d file(s) %s -> %s" % (count, local_directory, remote_directory))
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
    extension = os.path.splitext(local)[1].lower()
    source_name = os.path.basename(local)
    source_stem = os.path.splitext(source_name)[0] or "a"

    # --dir: build into a caller-chosen remote dir and leave artifacts there.
    # Otherwise use a unique temp dir so concurrent agents don't collide.
    requested_directory = args.dir
    if requested_directory:
        work_directory = requested_directory
        return_code, stdout, stderr = exec_command(
            client, vm, "mkdir -p %s" % shlex.quote(work_directory), timeout=30
        )
        if return_code != 0:
            die(
                "could not create --dir %s: %s"
                % (work_directory, stderr.decode(errors="replace"))
            )
    else:
        return_code, stdout, stderr = exec_command(
            client, vm, "mktemp -d /tmp/vmbuild.XXXXXX", timeout=30
        )
        if return_code != 0:
            die("mktemp failed: %s" % stderr.decode(errors="replace"))
        work_directory = stdout.decode().strip()
    # Keep artifacts when the caller named the dir (that's the point) or asked with --keep.
    keep = args.keep or bool(requested_directory)
    remote_source = posixpath.join(work_directory, source_name)

    # upload source (sftp with base64 fallback)
    sftp = _sftp_or_none(client)
    pushed = False
    if sftp is not None:
        try:
            sftp.put(local, remote_source)
            pushed = True
        except Exception:
            pushed = False
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    if not pushed:
        _push_b64(client, vm, local, remote_source)

    # Interpreted scripts authored on Windows carry CRLF, which breaks bash
    # (`exit 0\r: numeric argument required`) and shebang lines. Normalize to LF.
    if extension in (".sh", ".py"):
        exec_command(
            client, vm, "sed -i 's/\\r$//' " + shlex.quote(remote_source), timeout=30
        )

    remote_binary = posixpath.join(work_directory, source_stem)
    compile_command = _build_cmd(remote_source, extension, remote_binary)
    if compile_command is not None:
        return_code, stdout, stderr = exec_command(
            client, vm, compile_command, timeout=args.timeout, stream=True
        )
        if return_code != 0:
            sys.stderr.buffer.write(stderr)
            sys.stderr.buffer.flush()
            _cleanup(client, vm, work_directory, keep)
            die(
                "compile failed (rc=%d)" % return_code,
                code=return_code if return_code else 1,
            )
        target = remote_binary
    else:
        # interpreted: run the source itself
        if extension == ".py":
            target = "python3 " + shlex.quote(remote_source)
        elif extension == ".sh":
            target = "bash " + shlex.quote(remote_source)
        else:
            _cleanup(client, vm, work_directory, keep)
            die("don't know how to build/run %s" % extension)

    # A single --args value is a shell-style string: --args "1 2 3" means three
    # arguments. Multiple values pass through literally, so an argument with an
    # embedded space is still expressible: --args alpha "two words".
    program_arguments = list(args.args or [])
    if len(program_arguments) == 1:
        program_arguments = shlex.split(program_arguments[0])
    argument_string = " ".join(shlex.quote(argument) for argument in program_arguments)
    if compile_command is not None:
        run_command = "%s %s" % (shlex.quote(target), argument_string)
    else:
        run_command = "%s %s" % (target, argument_string)
    # Run from the build dir so relative paths the program opens/creates land
    # next to its artifacts (matters most with --dir).
    run_command = "cd %s && %s" % (shlex.quote(work_directory), run_command.strip())

    try:
        return_code, stdout, stderr = exec_command(
            client,
            vm,
            run_command,
            as_user=args.as_user,
            timeout=args.timeout,
            stream=True,
        )
    except TimeoutError as error:
        _cleanup(client, vm, work_directory, keep)
        die(str(error), EXIT_TIMEOUT)

    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    if stderr:
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
    _cleanup(client, vm, work_directory, keep)
    return return_code


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
    'meta=$(stat -c "%i %Y %s" -- "$p") || exit $?; '
    'if [ -f "$p" ]; then h=$(sha256sum -- "$p") || exit $?; '
    "h=${{h%% *}}; else h=-; fi; "
    'echo "$meta $h"; else echo MISSING; fi'
)


def _snapshot(client, vm, path):
    snapshot_command = _SNAP_SH.format(p=shlex.quote(path))
    return_code, stdout, stderr = exec_command(client, vm, snapshot_command, timeout=60)
    if return_code != 0:
        die(
            "snapshot failed for %s: %s"
            % (
                path,
                stderr.decode(errors="replace").strip() or "remote rc=%s" % return_code,
            )
        )
    return stdout.decode(errors="replace").strip()


def cmd_snap(client, vm, args):
    line = _snapshot(client, vm, args.path)
    print(line)
    return 0


def cmd_verify(client, vm, args):
    baseline = (args.baseline or "").strip()
    current = _snapshot(client, vm, args.path)

    baseline_missing = baseline == "" or baseline == "MISSING"
    current_missing = current == "MISSING"

    if current_missing:
        result = "MISSING" if baseline_missing else "MODIFIED"
    elif baseline_missing:
        result = "CREATED"
    elif current == baseline:
        result = "UNCHANGED"
    else:
        result = "MODIFIED"

    line = result
    if args.token is not None:
        found = False
        if not current_missing:
            return_code, stdout, stderr = exec_command(
                client,
                vm,
                "grep -qF -- %s %s" % (shlex.quote(args.token), shlex.quote(args.path)),
                timeout=60,
            )
            found = return_code == 0
        line += " TOKEN=%s" % ("present" if found else "absent")
    print(line)
    return 0


def _file_exists(client, vm, path, timeout):
    return_code, stdout, stderr = exec_command(
        client,
        vm,
        "if [ -e %s ]; then echo EXISTS; else echo MISSING; fi" % shlex.quote(path),
        timeout=timeout,
    )
    if return_code != 0 or stdout.strip() not in (b"EXISTS", b"MISSING"):
        die(
            "waitfile probe failed for %s: %s"
            % (
                path,
                stderr.decode(errors="replace").strip()
                or "invalid response (rc=%s)" % return_code,
            )
        )
    return stdout.strip() == b"EXISTS"


def cmd_waitfile(client, vm, args):
    path, timeout = args.path, args.timeout
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError()
        return budget

    try:
        if getattr(args, "remove_existing", False) and _file_exists(
            client, vm, path, remaining()
        ):
            return_code, stdout, stderr = exec_command(
                client, vm, "rm -f -- %s" % shlex.quote(path), timeout=remaining()
            )
            if return_code != 0:
                die(
                    "waitfile: could not remove pre-existing %s: %s"
                    % (path, stderr.decode(errors="replace").strip())
                )
            status("removed pre-existing %s; waiting for it to be recreated" % path)
        while True:
            if _file_exists(client, vm, path, remaining()):
                status("appeared: %s" % path)
                return 0
            time.sleep(min(1.0, remaining()))
    except TimeoutError:
        die("waitfile timed out after %ss: %s" % (timeout, path), EXIT_TIMEOUT)


# --- Port verbs (guest firewall) ---------------------------------------------
# The host can already reach the guest IP (that's how SSH gets in), so "opening a
# port" is only a guest-side firewall edit -- no host process, no state file. The
# rules live in the guest, which means `vm snapshot` bakes them in and `vm revert`
# drops them. Linux rules are matched by their own spec; Windows rules are named
# vm-port-<port>-<proto> so close/list can find them again.
def _sudo_exec(client, vm, cmd, timeout=60):
    """Run a command as root on a Linux guest; returns (rc, stdout, stderr) as text."""
    password = user_password(vm, vm.get("default_user"))
    return_code, stdout, stderr = exec_command(
        client,
        vm,
        "sudo -S -p '' %s" % cmd,
        timeout=timeout,
        stdin_data=(password + "\n").encode(),
    )
    return return_code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _linux_fw(client, vm):
    """Pick the guest's firewall: ufw when it's installed AND active, else iptables.

    An inactive ufw enforces nothing, so editing it would be a silent no-op --
    and its rules would override ours the moment it came up.
    """
    return_code, stdout, _ = _sudo_exec(client, vm, "ufw status", timeout=30)
    if return_code == 0 and "inactive" not in stdout.lower():
        return "ufw"
    return_code, _, stderr = _sudo_exec(client, vm, "iptables -S INPUT", timeout=30)
    if return_code == 0:
        return "iptables"
    die(
        "no usable firewall in the guest (ufw not active; iptables: %s)"
        % stderr.strip()
    )


def _clean_ps_err(text):
    """Flatten PowerShell's CLIXML stderr into one readable line.

    powershell.exe serializes stderr as CLIXML whenever it's redirected -- which
    it always is over SSH -- so a plain error arrives as a wall of XML wrapped
    around the message. Pull out the error strings and drop the parser trace.
    """
    if not text.lstrip().startswith("#< CLIXML"):
        return text
    body = html.unescape("".join(re.findall(r'<S S="Error">(.*?)</S>', text, re.S)))
    body = body.replace("_x000D_", "").replace("_x000A_", "\n")
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return " ".join(
        line
        for line in lines
        if not re.match(r"(At line:|\+|CategoryInfo|FullyQualified)", line)
    )


def _win_ps(client, vm, script, timeout=60):
    """Run PowerShell in a Windows guest via -EncodedCommand (no cmd.exe quoting)."""
    full = "$ErrorActionPreference='Stop';" + script
    encoded_script = base64.b64encode(full.encode("utf-16-le")).decode()
    return_code, stdout, stderr = exec_command(
        client,
        vm,
        "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encoded_script,
        timeout=timeout,
    )
    return (
        return_code,
        stdout.decode(errors="replace"),
        _clean_ps_err(stderr.decode(errors="replace")),
    )


def _port_apply(client, vm, action, port, proto):
    """Open or close one inbound port in the guest firewall. Returns the backend used."""
    if guest_os(vm) == "windows":
        rule = "vm-port-%d-%s" % (port, proto)
        if action == "open":
            # Reuse an existing rule instead of failing on a duplicate name, and
            # force Profile Any: VMware's NAT network is classified Public.
            script = (
                "$n='%s';"
                "if(Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue)"
                "{Set-NetFirewallRule -Name $n -Enabled True -Profile Any}"
                "else{New-NetFirewallRule -Name $n -DisplayName $n "
                "-Direction Inbound -Action Allow -Protocol %s -LocalPort %d "
                "-Profile Any|Out-Null}" % (rule, proto.upper(), port)
            )
        else:
            script = "Remove-NetFirewallRule -Name '%s'" % rule
        return_code, stdout, stderr = _win_ps(client, vm, script)
        if return_code != 0:
            die(
                "%s %d/%s failed: %s"
                % (action, port, proto, (stderr or stdout).strip())
            )
        return "windows firewall"

    firewall = _linux_fw(client, vm)
    if firewall == "ufw":
        command = "ufw %s %d/%s" % (
            "allow" if action == "open" else "delete allow",
            port,
            proto,
        )
    else:
        # -D takes the same spec as -I, so no rule numbers or comments to track.
        command = "iptables %s INPUT -p %s --dport %d -j ACCEPT" % (
            "-I" if action == "open" else "-D",
            proto,
            port,
        )
    return_code, stdout, stderr = _sudo_exec(client, vm, command)
    if return_code != 0:
        die("%s failed: %s" % (command, (stderr or stdout).strip()))
    return firewall


def _port_list(client, vm):
    """Return (backend, ["8080/tcp", ...]) of inbound ports the guest firewall allows."""
    if guest_os(vm) == "windows":
        # Only rules `vm port open` made: the name carries the port, and listing
        # every built-in Windows allow rule would be noise, not an answer.
        return_code, stdout, stderr = _win_ps(
            client,
            vm,
            "Get-NetFirewallRule -Name 'vm-port-*' "
            "-ErrorAction SilentlyContinue|"
            "ForEach-Object{($_.Name -replace '^vm-port-','')"
            " -replace '-','/'}",
        )
        if return_code != 0:
            die("could not list firewall rules: %s" % (stderr or stdout).strip())
        return "windows firewall", stdout.split()

    firewall = _linux_fw(client, vm)
    if firewall == "ufw":
        return_code, stdout, stderr = _sudo_exec(client, vm, "ufw status")
        # "8080/tcp   ALLOW   Anywhere" -- the v6 twin reads "8080/tcp (v6) ALLOW",
        # so anchoring ALLOW to the second column dedupes it for free.
        ports = [
            line.split()[0]
            for line in stdout.splitlines()
            if re.match(r"^\d+(/(tcp|udp))?\s+ALLOW\b", line)
        ]
    else:
        return_code, stdout, stderr = _sudo_exec(client, vm, "iptables -S INPUT")
        ports = [
            "%s/%s" % (match.group(2), match.group(1))
            for match in re.finditer(
                r"-p (tcp|udp).*?--dport (\d+).*?-j ACCEPT", stdout
            )
        ]
    if return_code != 0:
        die("could not list firewall rules: %s" % (stderr or stdout).strip())
    return firewall, ports


def cmd_port(client, vm, args):
    """port open|close|list -- inbound ports in the guest's own firewall."""
    if args.portverb == "list":
        firewall, ports = _port_list(client, vm)
        for port in ports:
            print(port)
        status("%d open (%s)" % (len(ports), firewall))
        return 0

    proto = args.proto.lower()
    if not 1 <= args.port <= 65535:
        die("port must be 1-65535, got %d" % args.port)
    firewall = _port_apply(client, vm, args.portverb, args.port, proto)
    status(
        "%s %d/%s on %s (%s)"
        % (
            "opened" if args.portverb == "open" else "closed",
            args.port,
            proto,
            vm.get("host"),
            firewall,
        )
    )
    return 0


# --- Host / vmrun ------------------------------------------------------------
def vmrun_path(cfg):
    path = cfg.get("vmrun")
    if not path:
        die("no 'vmrun' path in config")
    return path


def encryption_password(vm):
    """The VM's encryption/TPM password, or "" if the VM isn't encrypted.

    Windows 11 guests carry a vTPM, which VMware requires the VM be encrypted
    to hold; vmrun then needs this password (via -vp) just to open the VM.
    """
    if not vm:
        return ""
    return vm.get("encryption_password") or ""


def run_vmrun(cfg, vmrun_args, timeout=120, vm=None, timeout_is_error=True):
    """Invoke vmrun.exe with the given args; return (rc, stdout, stderr).

    When `vm` is encrypted, its password is injected via -vp so vmrun can open
    it. If vmrun reports the VM needs a password we didn't supply, fail with an
    actionable hint pointing at the config field rather than looping on it.
    """
    executable = vmrun_path(cfg)
    if not os.path.isfile(executable):
        die("vmrun.exe not found: %s" % executable)
    vm_password = encryption_password(vm)
    prefix = ["-vp", vm_password] if vm_password else []
    command = [executable] + prefix + vmrun_args
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        message = "vmrun timed out after %ss; check VMware and guest Tools" % timeout
        if timeout_is_error:
            die(message, EXIT_TIMEOUT)
        return EXIT_TIMEOUT, "", message
    except OSError as error:
        die("cannot execute vmrun %s: %s" % (executable, error))
    if (
        "a password is required"
        in ((result.stderr or "") + (result.stdout or "")).lower()
        and not vm_password
    ):
        die(
            "VM is encrypted (Windows 11 vTPM?) and vmrun cannot open it "
            'without the encryption password. Add "encryption_password": '
            '"<password>" to this VM\'s config block.',
            EXIT_ENV,
        )
    return result.returncode, result.stdout, result.stderr


def vmx_path(vm):
    path = vm.get("vmx")
    if not path:
        die("VM has no 'vmx' path configured")
    return path


# --- VMware Tools guest operations (no SSH; used to bootstrap Windows SSH) ----
# PowerShell that idempotently turns on the built-in OpenSSH Server. Runs elevated
# via vmrun's Tools channel (the guest user must be a local admin). Writes a result
# marker we copy back so we can tell setup apart from a silent no-op.
WINDOWS_SSH_SETUP_PS1 = r"""
$o = @()
try {
  $cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
  if ($cap.State -ne 'Installed') {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    $o += 'capability=installed'
  } else { $o += 'capability=present' }
} catch { $o += 'capability_err=' + $_.Exception.Message }
try {
  Set-Service -Name sshd -StartupType Automatic
  Start-Service sshd
  $o += 'sshd=' + (Get-Service sshd).Status
} catch { $o += 'sshd_err=' + $_.Exception.Message }
# A fresh Win11 VM on VMware NAT is usually classified as a Public network, but
# the OpenSSH capability's default inbound rule is scoped to Private/Domain, so
# port 22 stays blocked. Force the rule to exist, be enabled, and apply to ALL
# profiles -- don't just skip when a (wrongly-scoped) rule is already present.
try {
  $fw = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
  if ($fw) {
    Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Any
    $o += 'firewall=updated'
  } else {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
      -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 `
      -Profile Any | Out-Null
    $o += 'firewall=created'
  }
} catch { $o += 'firewall_err=' + $_.Exception.Message }
$o | Out-File -FilePath 'C:\Windows\Temp\vm_setup_ssh.out' -Encoding ascii
"""


def _vmrun_guest(cfg, vm, op_args, timeout=180):
    """Run a vmrun guest operation with this VM's guest credentials injected."""
    user = vm.get("default_user")
    if not user:
        die("VM has no default_user for guest operations")
    password = user_password(vm, user)
    args = ["-T", "ws", "-gu", user, "-gp", password] + op_args
    return run_vmrun(cfg, args, timeout=timeout, vm=vm)


# Probe path in a location any standard user can write, so a non-admin
# default_user doesn't fail the check for the wrong reason.
_EXEC_PROBE_PATH = "C:\\Users\\Public\\vm_exec_probe.txt"


def _guest_can_launch_programs(cfg, vm, wait=12):
    """Return True if VMware Tools can actually LAUNCH a program in the guest.

    A freshly-created Windows guest sometimes has a half-initialized Tools
    guest-ops channel: auth, queries (listProcessesInGuest) and file copies all
    work, but StartProgram silently no-ops -- and a wait-mode runProgramInGuest
    then blocks for the full timeout. We detect that here cheaply: fire a trivial
    program with -noWait (which can't hang) and see whether its marker file shows
    up. If it never does, program launch is dead.
    """
    vmx = vmx_path(vm)
    # Clear any stale marker (ignore "not found").
    _vmrun_guest(cfg, vm, ["deleteFileInGuest", vmx, _EXEC_PROBE_PATH], timeout=30)
    command = "C:\\Windows\\System32\\cmd.exe"
    _vmrun_guest(
        cfg,
        vm,
        [
            "runProgramInGuest",
            vmx,
            "-noWait",
            command,
            "/c",
            "echo ok> " + _EXEC_PROBE_PATH,
        ],
        timeout=30,
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        return_code, stdout, stderr = _vmrun_guest(
            cfg, vm, ["fileExistsInGuest", vmx, _EXEC_PROBE_PATH], timeout=30
        )
        if "the file exists" in (stdout or "").lower():
            _vmrun_guest(
                cfg, vm, ["deleteFileInGuest", vmx, _EXEC_PROBE_PATH], timeout=30
            )
            return True
        time.sleep(2)
    return False


def bootstrap_windows_ssh(cfg, vm):
    """Enable OpenSSH in a Windows guest over VMware Tools, then wait for SSH.

    Returns True once SSH accepts a connection, False otherwise. Idempotent.
    """
    if guest_os(vm) != "windows":
        return False
    vmx = vmx_path(vm)
    # Tools must be up for guest ops to work at all.
    return_code, stdout, stderr = run_vmrun(
        cfg, ["checkToolsState", vmx], timeout=60, vm=vm
    )
    state = stdout.strip()
    if state != "running":
        die(
            "VMware Tools not running in guest (state=%s); can't bootstrap SSH"
            % (state or "?")
        )

    # Preflight: confirm Tools can actually launch a program before we copy and
    # run the (wait-mode) setup script, which would otherwise hang for minutes.
    if not _guest_can_launch_programs(cfg, vm):
        die(
            "VMware Tools is up and authenticates, but cannot launch programs in "
            "the guest (StartProgram no-ops) -- so SSH can't be bootstrapped over "
            "Tools. This is common on a freshly-created Windows VM. Fix: reboot "
            "the guest and retry; if it persists, reinstall/repair VMware Tools "
            "and reboot. Workaround: enable OpenSSH manually in the guest "
            "(Add-WindowsCapability OpenSSH.Server; Start-Service sshd) -- once "
            "sshd is up, the SSH verbs work without Tools.",
            EXIT_ENV,
        )

    guest_script_path = "C:\\Windows\\Temp\\vm_setup_ssh.ps1"
    file_descriptor, temporary_path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(
            file_descriptor, "w", encoding="ascii", errors="replace"
        ) as file:
            file.write(WINDOWS_SSH_SETUP_PS1)
        return_code, stdout, stderr = _vmrun_guest(
            cfg,
            vm,
            ["copyFileFromHostToGuest", vmx, temporary_path, guest_script_path],
            120,
        )
        if return_code != 0:
            die(
                "could not copy setup script into guest: %s"
                % (stderr or stdout).strip()
            )
    finally:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass

    powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    return_code, stdout, stderr = _vmrun_guest(
        cfg,
        vm,
        [
            "runProgramInGuest",
            vmx,
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-NoProfile",
            "-File",
            guest_script_path,
        ],
        timeout=300,
    )
    if return_code != 0:
        die("OpenSSH setup script failed in guest: %s" % (stderr or stdout).strip())

    host = vm.get("host")
    if host and _wait_for_ssh(vm, host, timeout=90):
        return True
    return False


def cmd_vm_snapshot(cfg, vm, args):
    snap = args.snap or vm.get("snapshot")
    if not snap:
        die("no snapshot name given and no 'snapshot' in config")
    return_code, stdout, stderr = run_vmrun(
        cfg, ["snapshot", vmx_path(vm), snap], vm=vm
    )
    if return_code != 0:
        die("vmrun snapshot failed: %s" % (stderr or stdout).strip(), return_code or 1)
    status("snapshot taken: %s" % snap)
    return 0


def cmd_vm_revert(cfg, vm, args):
    snap = args.snap or vm.get("snapshot")
    if not snap:
        die("no snapshot name given and no 'snapshot' in config")
    return_code, stdout, stderr = run_vmrun(
        cfg, ["revertToSnapshot", vmx_path(vm), snap], vm=vm
    )
    if return_code != 0:
        die("vmrun revert failed: %s" % (stderr or stdout).strip(), return_code or 1)
    status("reverted to snapshot: %s" % snap)
    return 0


def _vm_running(cfg, vm, timeout=30):
    return_code, stdout, stderr = run_vmrun(cfg, ["list"], timeout=timeout)
    if return_code != 0:
        die("vmrun list failed: %s" % (stderr or stdout).strip(), return_code)

    def normalized_path(path):
        return os.path.normcase(os.path.abspath(path.strip()))

    target_path = normalized_path(vmx_path(vm))
    for line in stdout.splitlines():
        if line.strip().lower().endswith(".vmx"):
            if normalized_path(line) == target_path:
                return True
    return False


def cmd_vm_start(cfg, vm, args):
    if not _vm_running(cfg, vm):
        return_code, stdout, stderr = run_vmrun(
            cfg, ["start", vmx_path(vm), "nogui"], vm=vm
        )
        if return_code != 0:
            die("vmrun start failed: %s" % (stderr or stdout).strip(), return_code)
    if getattr(args, "no_wait", False):
        status("powered on")
        return 0
    deadline = time.monotonic() + getattr(args, "timeout", 180)
    ip = _get_ip(cfg, vm, retries=1000, timeout=max(0, deadline - time.monotonic()))
    if not ip:
        die("start timed out waiting for guest IP (VMware Tools?)", EXIT_TIMEOUT)
    vm["host"] = ip
    if not _wait_for_ssh(vm, ip, timeout=max(0, deadline - time.monotonic())):
        die("start timed out waiting for SSH at %s" % ip, EXIT_TIMEOUT)
    status("started; SSH ready at %s" % ip)
    return 0


def cmd_vm_stop(cfg, vm, args):
    if not _vm_running(cfg, vm):
        status("already stopped")
        return 0
    mode = "hard" if getattr(args, "hard", False) else "soft"
    return_code, stdout, stderr = run_vmrun(cfg, ["stop", vmx_path(vm), mode], vm=vm)
    if return_code != 0:
        die(
            "vmrun stop failed: %s; use stop --hard to force power off"
            % (stderr or stdout).strip(),
            return_code,
        )
    deadline = time.monotonic() + getattr(args, "timeout", 180)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            die(
                "stop timed out waiting for power off; use stop --hard to force power off",
                EXIT_TIMEOUT,
            )
        if not _vm_running(cfg, vm, timeout=min(3, remaining)):
            status("stopped")
            return 0
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def cmd_vm_list(cfg, vm, args):
    return_code, stdout, stderr = run_vmrun(cfg, ["list"])
    if return_code != 0:
        die("vmrun list failed: %s" % (stderr or stdout).strip(), return_code or 1)
    sys.stdout.write(stdout)
    return 0


def cmd_vm_snapshots(cfg, vm, args):
    return_code, stdout, stderr = run_vmrun(cfg, ["listSnapshots", vmx_path(vm)], vm=vm)
    if return_code != 0:
        die(
            "vmrun listSnapshots failed: %s" % (stderr or stdout).strip(),
            return_code or 1,
        )
    sys.stdout.write(stdout)
    return 0


def _get_ip(cfg, vm, retries=30, delay=2, timeout=60):
    """Poll Tools within one total budget, accepting valid IPv4 or IPv6 only."""
    deadline = time.monotonic() + timeout
    for attempt in range(retries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        return_code, stdout, stderr = run_vmrun(
            cfg,
            ["getGuestIPAddress", vmx_path(vm)],
            timeout=min(3, remaining),
            vm=vm,
            timeout_is_error=False,
        )
        if return_code == 0:
            try:
                return str(ipaddress.ip_address(stdout.strip()))
            except ValueError:
                pass
        if "not powered on" in (stdout + stderr).lower():
            break
        if attempt + 1 < retries:
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    return None


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
    target = dict(vm, host=host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            client = _ssh_open(target, timeout=min(CONNECT_TIMEOUT, remaining / 3))
            client.close()
            return True
        except (
            paramiko.AuthenticationException,
            ValueError,
            paramiko.BadHostKeyException,
        ) as error:
            die(
                "SSH readiness check failed: %s; check guest credentials/config" % error
            )
        except Exception:
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    return False


def cmd_vm_reset(cfg, vm, vm_name, args, config_path):
    snap = vm.get("snapshot")
    if not snap:
        die("no 'snapshot' configured for reset")
    return_code, stdout, stderr = run_vmrun(
        cfg, ["revertToSnapshot", vmx_path(vm), snap], vm=vm
    )
    if return_code != 0:
        die("revert failed: %s" % (stderr or stdout).strip(), return_code or 1)
    status("reverted to %s" % snap)
    cmd_vm_start(cfg, vm, argparse.Namespace(no_wait=True))
    status("powered on; discovering IP...")
    ip = _get_ip(cfg, vm)
    if not ip:
        die("VM booted but no guest IP (Tools?)", EXIT_ENV)
    cfg["vms"][vm_name]["host"] = ip
    atomic_write_json(config_path, cfg)
    status("IP=%s (saved); waiting for SSH..." % ip)
    if guest_os(vm) == "windows":
        # A reverted Windows guest may need OpenSSH started again; bootstrap is idempotent.
        if not _wait_for_ssh(vm, ip, timeout=30) and not bootstrap_windows_ssh(cfg, vm):
            die("VM up at %s but SSH never came up" % ip, EXIT_ENV)
    elif not _wait_for_ssh(vm, ip):
        die("VM up at %s but SSH never came up" % ip, EXIT_ENV)
    status("reset complete: %s reachable at %s" % (vm_name, ip))
    return 0


def cmd_vm_setup_ssh(cfg, vm, vm_name, args):
    """Enable OpenSSH in the guest so the SSH verbs work. Windows-only; idempotent."""
    if guest_os(vm) != "windows":
        status(
            "%s is a %s guest; SSH is native - nothing to set up"
            % (vm_name, guest_os(vm))
        )
        return 0
    if not vm.get("host"):
        die("VM has no host/IP configured (run: vm --vm %s ip --save)" % vm_name)
    if bootstrap_windows_ssh(cfg, vm):
        status(
            "OpenSSH is up on %s (%s); SSH verbs are ready" % (vm_name, vm.get("host"))
        )
        return 0
    die("bootstrap ran but SSH did not come up on %s" % vm.get("host"), EXIT_ENV)


# --- Provisioning: stage a host folder of tools into the guest ---------------
# Convention over config: drop files in provision/<vm-name>/ (or provision/<os>/)
# beside the config. On first connect they're synced to the guest tools dir and made
# executable; an optional setup.sh (Linux) / setup.ps1 (Windows, run elevated over
# VMware Tools) does anything a plain copy can't. A hash marker skips unchanged runs.
def provision_local_dir(vm, vm_name):
    """Host folder to stage, or None. Prefers provision/<vm-name>/, then provision/<os>/.
    Anchored to PROJECT_DIR (the config's directory), so it works when installed."""
    base = os.path.join(PROJECT_DIR, "provision")
    for folder_name in (vm_name, guest_os(vm)):
        directory = os.path.join(base, folder_name)
        if os.path.isdir(directory):
            return directory
    return None


def _tree_hash(localdir):
    """Stable hash of a folder's relative paths + contents (order-independent)."""
    tree_hash = hashlib.sha256()
    for root, dirs, names in os.walk(localdir):
        dirs.sort()
        for filename in sorted(names):
            file_path = os.path.join(root, filename)
            relative_path = os.path.relpath(file_path, localdir).replace(os.sep, "/")
            tree_hash.update(relative_path.encode())
            with open(file_path, "rb") as file:
                tree_hash.update(hashlib.sha256(file.read()).digest())
    return tree_hash.hexdigest()


def _run_setup_script(cfg, vm, client, tools, localdir):
    """Run the optional setup script once (Linux: setup.sh over SSH; Windows:
    setup.ps1 over the elevated VMware Tools channel)."""
    if guest_os(vm) == "windows":
        if not os.path.isfile(os.path.join(localdir, "setup.ps1")):
            return
        winpath = (posixpath.join(tools, "setup.ps1")).replace("/", "\\")
        powershell = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
        status("running setup.ps1 (elevated, via VMware Tools)...")
        return_code, stdout, stderr = _vmrun_guest(
            cfg,
            vm,
            [
                "runProgramInGuest",
                vmx_path(vm),
                powershell,
                "-ExecutionPolicy",
                "Bypass",
                "-NoProfile",
                "-File",
                winpath,
            ],
            timeout=1800,
        )
        if return_code != 0:
            die(
                "setup.ps1 failed in guest: %s" % (stderr or stdout).strip(),
                return_code or 1,
            )
    else:
        if not os.path.isfile(os.path.join(localdir, "setup.sh")):
            return
        status("running setup.sh...")
        return_code, stdout, stderr = exec_command(
            client,
            vm,
            "bash %s" % shlex.quote(posixpath.join(tools, "setup.sh")),
            timeout=1800,
        )
        if stderr:
            sys.stderr.buffer.write(stderr)
            sys.stderr.buffer.flush()
        if return_code != 0:
            die("setup.sh failed (rc=%d)" % return_code, return_code or 1)


def provision_guest(cfg, vm, vm_name, client, force=False, verbose=True):
    """Ensure provision/<vm|os>/ is staged into the guest tools dir. Returns True
    if it did work, False if nothing to do / already current. Idempotent."""
    local_directory = provision_local_dir(vm, vm_name)
    if not local_directory:
        return False
    tools = tools_remote(vm)
    marker = posixpath.join(tools, ".provisioned")
    desired_hash = _tree_hash(local_directory)

    sftp = _sftp_or_none(client)
    if sftp is None:
        die("provisioning needs SFTP but it is unavailable")
    try:
        current_hash = None
        try:
            with sftp.open(marker, "r") as file:
                current_hash = file.read().decode(errors="replace").strip()
        except IOError:
            current_hash = None
        if current_hash == desired_hash and not force:
            if verbose:
                status("tools already current (%s)" % os.path.basename(local_directory))
            return False

        _ensure_remote_dir(client, vm, sftp, tools)
        files = []
        for root, dirs, names in os.walk(local_directory):
            dirs.sort()
            for filename in sorted(names):
                files.append(os.path.join(root, filename))
        created_directories = set()
        for local_file in files:
            relative_path = os.path.relpath(local_file, local_directory).replace(
                os.sep, "/"
            )
            remote_file = posixpath.join(tools, relative_path)
            parent = posixpath.dirname(remote_file)
            if parent and parent not in created_directories:
                _ensure_remote_dir(client, vm, sftp, parent)
                created_directories.add(parent)
            sftp.put(local_file, remote_file)
        status("staged %d file(s) -> %s" % (len(files), tools))

        if guest_os(vm) != "windows":
            exec_command(
                client,
                vm,
                "find %s -type f -exec chmod +x {} +" % shlex.quote(tools),
                timeout=60,
            )

        _run_setup_script(cfg, vm, client, tools, local_directory)

        with sftp.open(marker, "w") as file:
            file.write(desired_hash)
        status("provisioned %s from %s" % (vm_name, os.path.basename(local_directory)))
        return True
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def maybe_auto_provision(cfg, vm, vm_name, client):
    """Called after a successful connect for guest verbs: stage tools if a
    provision folder exists and the guest is missing/out of date. A hard error
    (die) in a setup step propagates; softer failures degrade to a warning."""
    if provision_local_dir(vm, vm_name) is None:
        return
    try:
        provision_guest(cfg, vm, vm_name, client, force=False, verbose=False)
    except SystemExit:
        raise
    except Exception as error:
        status("warning: auto-provision skipped: %s" % error)


def cmd_vm_provision(cfg, vm, vm_name, args):
    if provision_local_dir(vm, vm_name) is None:
        die(
            "no provision folder: create provision/%s/ or provision/%s/ beside vmconfig.json"
            % (vm_name, guest_os(vm))
        )
    client = ssh_connect(cfg, vm)
    try:
        provision_guest(cfg, vm, vm_name, client, force=args.force, verbose=True)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return 0


def _probe_user_sudo(vm, uname):
    """SSH in as `uname` with their own password and test their own sudo rights.

    True = the user can sudo (correct password accepted by sudo -S).
    Raises on SSH/login failure (can't tell either way).
    """
    password = user_password(vm, uname)
    client = _ssh_open(vm, user=uname)
    try:
        return_code, stdout, stderr = exec_command(
            client,
            vm,
            "sudo -S -p '' true",
            timeout=30,
            stdin_data=(password + "\n").encode(),
        )
        return return_code == 0
    finally:
        try:
            client.close()
        except Exception:
            pass


def cmd_vm_doctor(cfg, vm, vm_name, args, config_path):
    """Per-check PASS/FAIL health report."""
    checks = []

    # config already parsed to get here
    checks.append(("config parses", True, config_path))

    executable = cfg.get("vmrun")
    executable_exists = bool(executable) and os.path.isfile(executable)
    checks.append(("vmrun.exe exists", executable_exists, executable or "(unset)"))

    vmx = vm.get("vmx")
    vmx_exists = bool(vmx) and os.path.isfile(vmx)
    checks.append(("vmx exists", vmx_exists, vmx or "(unset)"))

    # Discover the current address before SSH, using the same lookup as `vm ip`.
    # Save changed addresses so subsequent commands can connect directly.
    if executable_exists and vmx_exists:
        configured_host = vm.get("host")
        discovered_ip = _get_ip(cfg, vm)
        if discovered_ip:
            ip_detail = discovered_ip
            if discovered_ip != configured_host:
                vm["host"] = discovered_ip
                cfg["vms"][vm_name]["host"] = discovered_ip
                atomic_write_json(config_path, cfg)
                ip_detail += " (was: %s; saved to %s)" % (
                    configured_host or "unset",
                    config_path,
                )
            checks.append(("guest IP", True, ip_detail))
        else:
            checks.append(
                (
                    "guest IP",
                    False,
                    "could not determine IP (VM powered on; Tools running?)",
                )
            )
    else:
        checks.append(("guest IP", False, "requires valid vmrun and vmx paths"))

    # SSH connect
    ssh_ok = False
    ssh_detail = ""
    client = None
    try:
        host = vm.get("host")
        user = vm.get("default_user")
        client = _ssh_open(vm)
        ssh_ok = True
        ssh_detail = "%s@%s" % (user, host)
    except Exception as error:
        ssh_detail = str(error)
        if (
            executable_exists
            and vmx_exists
            and not isinstance(error, (paramiko.AuthenticationException, ValueError))
        ):
            if guest_os(vm) == "windows":
                ssh_detail += " (run: vm --vm %s setup-ssh)" % vm_name
    checks.append(("ssh connects", ssh_ok, ssh_detail))

    users = vm.get("users") or {}
    if guest_os(vm) == "windows":
        # Windows guests have no sudo/`--as`; just confirm SSH command execution works.
        if ssh_ok:
            try:
                return_code, stdout, stderr = exec_command(
                    client, vm, "whoami", timeout=30
                )
                reported_username = stdout.decode(errors="replace").strip()
                checks.append(
                    (
                        "ssh exec works",
                        return_code == 0 and bool(reported_username),
                        reported_username or "rc=%d" % return_code,
                    )
                )
            except Exception as error:
                checks.append(("ssh exec works", False, str(error)))
        else:
            checks.append(("ssh exec works", False, "ssh down"))
    elif ssh_ok:
        # per configured user: (a) `--as USER` impersonation works from default_user,
        # (b) the user's own sudo rights match the config's `sudo` flag
        for username in sorted(users):
            try:
                return_code, stdout, stderr = exec_command(
                    client, vm, "id -un", as_user=username, timeout=30
                )
                reported_username = stdout.decode(errors="replace").strip()
                ok = return_code == 0 and reported_username == username
                detail = (
                    reported_username
                    if reported_username
                    else (
                        stderr.decode(errors="replace").strip() or "rc=%d" % return_code
                    )
                )
            except Exception as error:
                ok = False
                detail = str(error)
            checks.append(("--as %s works" % username, ok, detail))

            if "sudo" in users[username]:
                expects_sudo = bool(users[username]["sudo"])
                try:
                    has_sudo = _probe_user_sudo(vm, username)
                    ok = has_sudo == expects_sudo
                    detail = "has sudo" if has_sudo else "no sudo"
                    if not ok:
                        detail += " but config says sudo=%s" % str(expects_sudo).lower()
                except Exception as error:
                    ok = False
                    detail = "probe failed: %s" % error
                checks.append(("sudo as %s" % username, ok, detail))
    else:
        for username in sorted(users):
            checks.append(("--as %s works" % username, False, "ssh down"))
            if "sudo" in users[username]:
                checks.append(("sudo as %s" % username, False, "ssh down"))

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
def default_wsl_distro():
    """Return the name of the default WSL distro, or None if none is set.

    Parses `wsl -l -v` (UTF-16 encoded on Windows); the default distro is the
    row flagged with a leading `*`."""
    try:
        result = subprocess.run(["wsl", "-l", "-v"], capture_output=True, timeout=15)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    # wsl.exe emits UTF-16LE; decode leniently and strip NULs either way.
    try:
        stdout = result.stdout.decode("utf-16-le")
    except (UnicodeDecodeError, LookupError):
        stdout = result.stdout.decode("utf-8", "ignore")
    stdout = stdout.replace("\x00", "")
    for line in stdout.splitlines():
        line = line.rstrip()
        if line.lstrip().startswith("*"):
            # e.g. "* Ubuntu-24.04    Running    2"
            parts = line.lstrip("* ").split()
            if parts:
                return parts[0]
    return None


def wsl_mount_point(vm):
    """Per-host mount point under the WSL user's home (writable, unlike /mnt)."""
    return "$HOME/vmstaging_%s" % vm.get("host", "vm").replace(".", "_")


def cmd_mount(cfg, vm, args):
    distro = vm.get("wsl_distro") or default_wsl_distro()
    if not distro:
        die(
            "no wsl_distro configured and no default WSL distro found "
            "(is WSL installed? `wsl -l -v`)"
        )
    host = vm.get("host")
    user = vm.get("default_user")
    password = user_password(vm, user)
    remote = vm.get("staging_remote")
    if not remote:
        die("staging_remote must be set for mount")
    # Keep the mount path stable for umount, even when SSH recovers a stale IP
    # for this invocation without saving it to config.
    mount_point = wsl_mount_point(vm)
    # sshfs cannot create the remote directory; if staging_remote doesn't exist
    # yet (e.g. before the first push/sync) the mount fails with a confusing
    # "No such file or directory". Ensure it exists first.
    client = ssh_connect(cfg, vm)
    try:
        sftp = None
        try:
            sftp = client.open_sftp()
        except Exception:
            sftp = None
        _ensure_remote_dir(client, vm, sftp, remote)
    finally:
        try:
            client.close()
        except Exception:
            pass
    host = vm.get("host")
    # $HOME is left unquoted so bash expands it; the rest is quoted.
    sshfs = (
        "mkdir -p %s && sshfs -o password_stdin,"
        "StrictHostKeyChecking=no,reconnect -p %d %s@%s:%s %s"
        % (
            mount_point,
            vm.get("ssh_port", 22),
            shlex.quote(user),
            shlex.quote(host),
            shlex.quote(remote),
            mount_point,
        )
    )
    try:
        result = subprocess.run(
            ["wsl", "-d", distro, "bash", "-c", sshfs],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        die("wsl mount timed out after 60s", EXIT_TIMEOUT)
    except OSError as error:
        die("wsl mount failed: %s" % error)
    if result.returncode != 0:
        die("sshfs mount failed: %s" % (result.stderr or result.stdout).strip())
    status("mounted %s:%s at %s (wsl %s)" % (host, remote, mount_point, distro))
    return 0


def cmd_umount(cfg, vm, args):
    distro = vm.get("wsl_distro") or default_wsl_distro()
    if not distro:
        die(
            "no wsl_distro configured and no default WSL distro found "
            "(is WSL installed? `wsl -l -v`)"
        )
    mount_point = wsl_mount_point(vm)
    try:
        result = subprocess.run(
            ["wsl", "-d", distro, "bash", "-c", "fusermount -u %s" % mount_point],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as error:
        die("wsl umount failed: %s" % error)
    if result.returncode != 0:
        die("umount failed: %s" % (result.stderr or result.stdout).strip())
    status("unmounted %s" % mount_point)
    return 0


# --- Argument parsing --------------------------------------------------------
class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        die(message)


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1-65535")
    return number


def _common_options(parser, root=False):
    # Suppressed child defaults preserve values supplied before the command.
    default = None if root else argparse.SUPPRESS
    parser.add_argument(
        "--vm", default=default, help="named VM (default: config default_vm)"
    )
    parser.add_argument(
        "--config",
        default=default,
        help="config path (else VM_CONFIG, nearest parent config, then home)",
    )
    parser.add_argument(
        "--no-provision",
        action="store_true",
        default=False if root else argparse.SUPPRESS,
        help="skip automatic tool staging/setup for guest commands",
    )


def _command(subparsers, name, **kwargs):
    parser = subparsers.add_parser(name, **kwargs)
    _common_options(parser)
    return parser


def _host_commands(subparsers):
    for name, help_text in (
        ("snapshot", "take a snapshot (default: config snapshot)"),
        ("revert", "revert to a snapshot (default: config snapshot)"),
        ("reset", "revert to clean snapshot, boot, refresh IP and wait for SSH"),
        ("start", "power on"),
        ("stop", "power off"),
        ("list", "list running VMs (no VM selection needed)"),
        ("snapshots", "list this VM's snapshots"),
        ("ip", "discover guest IP"),
        ("doctor", "check config, VMware paths, SSH and sudo"),
        ("setup-ssh", "(Windows) enable OpenSSH over VMware Tools"),
        ("provision", "stage provision/<vm|os>/ into the guest tools dir"),
    ):
        parser = _command(subparsers, name, help=help_text)
        if name in {"snapshot", "revert"}:
            parser.add_argument("snap", nargs="?")
        elif name in {"start", "stop"}:
            parser.add_argument(
                "--timeout",
                type=_positive_int,
                default=180,
                help="readiness/power-off wait timeout (s)",
            )
            if name == "start":
                parser.add_argument(
                    "--no-wait",
                    action="store_true",
                    help="return after power on without waiting for SSH",
                )
            else:
                parser.add_argument(
                    "--hard",
                    action="store_true",
                    help="force power off instead of graceful shutdown",
                )
        elif name == "ip":
            parser.add_argument(
                "--save", action="store_true", help="write IP back to config"
            )
        elif name == "provision":
            parser.add_argument(
                "--force", action="store_true", help="re-stage even if unchanged"
            )


def build_parser():
    parser = CLIParser(
        prog="vm",
        description="Run commands and transfer files over SSH; manage VMware guests with vmrun.",
        epilog='Examples: vm run "whoami" | vm doctor --vm NAME | vm ip --save',
    )
    _common_options(parser, root=True)
    commands = parser.add_subparsers(dest="verb", required=True)

    # run
    command_parser = _command(
        commands, "run", help="run a command on the guest over SSH"
    )
    command_parser.add_argument("command", help="command string to run")
    command_parser.add_argument(
        "--as", dest="as_user", metavar="USER", help="run as another user via sudo"
    )
    command_parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=DEFAULT_TIMEOUT,
        help="command timeout (s)",
    )

    # push
    command_parser = _command(
        commands, "push", help="upload files/dirs/globs (SFTP, base64 fallback)"
    )
    command_parser.add_argument(
        "--dest",
        metavar="REMOTE",
        help="explicit destination; every positional path is a source",
    )
    command_parser.add_argument(
        "paths",
        nargs="+",
        metavar="SRC... [DEST]",
        help="SRC may be a file, directory (recursed), or glob (docs/*.txt, "
        "src/**/*.py). Sources keep their relative path under the destination "
        "(default: staging_remote), so `push docs/*.txt` recreates docs/ remotely. "
        "With 2+ args a trailing path that isn't local is an explicit remote DEST; "
        "a single file + DEST is a literal rename.",
    )

    # pull
    command_parser = _command(
        commands, "pull", help="download a file (SFTP, base64 fallback)"
    )
    command_parser.add_argument("remote")
    command_parser.add_argument(
        "local", nargs="?", help="local path (default: ./basename)"
    )

    # sync
    command_parser = _command(
        commands, "sync", help="bulk push a directory (defaults from config staging)"
    )
    command_parser.add_argument("localdir", nargs="?")
    command_parser.add_argument("remotedir", nargs="?")

    # build-run
    command_parser = _command(
        commands, "build-run", help="(Linux) push, compile on VM, run, print output+rc"
    )
    command_parser.add_argument(
        "source", help="local source file (.c/.cpp compiled; .sh/.py run directly)"
    )
    command_parser.add_argument(
        "--as", dest="as_user", metavar="USER", help="run as another user via sudo"
    )
    command_parser.add_argument(
        "--args",
        nargs=argparse.REMAINDER,
        default=[],
        help="arguments passed to the program (must be last); a single "
        'quoted value is split shell-style: --args "1 2 3" = 3 args',
    )
    command_parser.add_argument(
        "--keep", action="store_true", help="keep the temp build dir on the VM"
    )
    command_parser.add_argument(
        "--dir",
        metavar="REMOTE",
        help="build in this remote dir (created if needed) and leave artifacts there, "
        "instead of a temp dir",
    )
    command_parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=DEFAULT_TIMEOUT,
        help="build/run timeout (s)",
    )

    # snap
    command_parser = _command(
        commands, "snap", help="(Linux) print 'inode mtime size sha256' snapshot"
    )
    command_parser.add_argument("path")

    # verify
    command_parser = _command(
        commands, "verify", help="(Linux) compare path to a baseline snap-line"
    )
    command_parser.add_argument("path")
    command_parser.add_argument(
        "--baseline", required=True, help="the snap-line to compare against"
    )
    command_parser.add_argument(
        "--token", help="also report whether this token appears in file content"
    )

    # waitfile
    command_parser = _command(
        commands,
        "waitfile",
        help="(Linux) wait for a file; existing files satisfy the wait",
    )
    command_parser.add_argument("path")
    command_parser.add_argument(
        "--remove-existing",
        action="store_true",
        help="delete the existing file first, then wait for recreation",
    )
    command_parser.add_argument(
        "--timeout", type=_positive_int, default=DEFAULT_TIMEOUT, help="max wait (s)"
    )

    # port (guest firewall)
    command_parser = _command(
        commands, "port", help="open/close/list inbound ports in the guest firewall"
    )
    port_commands = command_parser.add_subparsers(dest="portverb", required=True)
    for action, help_text in (
        ("open", "allow inbound traffic to a port"),
        ("close", "remove the rule `port open` added"),
    ):
        port_parser = _command(port_commands, action, help=help_text)
        port_parser.add_argument("port", type=_port_number)
        port_parser.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    _command(port_commands, "list", help="list inbound ports the guest firewall allows")

    # vm (host control)
    _host_commands(commands)
    command_parser = _command(
        commands, "vm", help="compatibility spelling: vm vm <host command>"
    )
    host_commands = command_parser.add_subparsers(dest="vmverb", required=True)
    _host_commands(host_commands)

    # mount / umount (optional WSL)
    _command(commands, "mount", help="(optional) sshfs live-bind staging dir via WSL")
    _command(commands, "umount", help="(optional) unmount the WSL sshfs bind")

    # docs (no config needed)
    command_parser = _command(
        commands, "docs", help="print the packaged README (usage reference)"
    )
    command_parser.add_argument(
        "--skill",
        action="store_true",
        help="print the vm-recovery SKILL.md instead of the README",
    )
    command_parser.add_argument(
        "--path",
        action="store_true",
        help="print the file's path instead of its contents",
    )
    command_parser.add_argument(
        "--install-skill",
        metavar="DIR",
        nargs="?",
        const=".",
        help="write SKILL.md to DIR/.claude/skills/vm-recovery/ (default: .)",
    )

    return parser


CONFIG_NAME = "vmconfig.json"


def config_candidates():
    """Where an implicit config may live, nearest first.

    The working directory, then each parent up to the filesystem root, then the
    home directory. Walking up means `vm` works from any subdirectory of a
    project; the home copy is the fallback for running outside one entirely.
    """
    paths = []
    directory = os.getcwd()
    while True:
        paths.append(os.path.join(directory, CONFIG_NAME))
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    home = os.path.expanduser("~")
    paths.append(os.path.join(home, CONFIG_NAME))
    paths.append(os.path.join(home, "." + CONFIG_NAME))
    return paths


def resolve_config_path(args):
    """--config wins, then $VM_CONFIG, then the nearest config found by searching up.

    Whichever file wins also sets PROJECT_DIR, so provision/ and staging/ anchor
    beside the config rather than wherever the command happened to be run.
    """
    if args.config:
        return args.config
    environment_path = os.environ.get("VM_CONFIG")
    if environment_path:
        return environment_path
    for path in config_candidates():
        if os.path.isfile(path):
            return path
    die(
        "no %s found in %s, any parent directory, or %s\n"
        "first-time setup: run `vm-init` here to create %s, "
        "then run: vm doctor"
        % (CONFIG_NAME, os.getcwd(), os.path.expanduser("~"), CONFIG_NAME)
    )


# Verbs that need an SSH client vs those that are host-only.
GUEST_HANDLERS = {
    "run": cmd_run,
    "push": cmd_push,
    "pull": cmd_pull,
    "sync": cmd_sync,
    "build-run": cmd_build_run,
    "snap": cmd_snap,
    "verify": cmd_verify,
    "waitfile": cmd_waitfile,
    "port": cmd_port,
}
HOST_HANDLERS = {
    "snapshot": cmd_vm_snapshot,
    "revert": cmd_vm_revert,
    "start": cmd_vm_start,
    "stop": cmd_vm_stop,
    "list": cmd_vm_list,
    "snapshots": cmd_vm_snapshots,
}
HOST_VERBS = set(HOST_HANDLERS) | {"reset", "ip", "doctor", "setup-ssh", "provision"}


def write_stdout_utf8(text):
    """Write text to stdout as UTF-8, bypassing the console's legacy codepage.

    The docs contain non-ASCII (arrows, dashes) that a cp1252 stdout can't encode.
    """
    output_buffer = getattr(sys.stdout, "buffer", None)
    if output_buffer is None:  # stdout replaced (tests, pipes) -- best effort
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    output_buffer.write(text.encode("utf-8"))
    output_buffer.flush()


def cmd_docs(args):
    """Print (or install) documentation shipped inside the package."""
    if args.install_skill is not None:
        try:
            destination = docs.install_skill(args.install_skill)
        except (IOError, OSError) as error:
            die("could not install skill: %s" % error)
        status("installed %s" % destination)
        return 0

    name = "skill" if args.skill else "readme"
    if args.path:
        path = docs.doc_path(name)
        if path is None:
            die("%s is not packaged with this install" % docs.DOCS[name])
        write_stdout_utf8(path + "\n")
        return 0
    text = docs.read_doc(name)
    if text is None:
        die("%s is not packaged with this install" % docs.DOCS[name])
    write_stdout_utf8(text if text.endswith("\n") else text + "\n")
    return 0


def main(argv=None):
    global PROJECT_DIR
    parser = build_parser()
    args = parser.parse_args(argv)

    # docs is self-contained: no config, no VM, works from any directory.
    if args.verb == "docs":
        return cmd_docs(args)

    config_path = resolve_config_path(args)
    PROJECT_DIR = os.path.dirname(os.path.abspath(config_path))
    config = load_config(config_path)
    verb = args.verb
    if verb == "vm":
        host_verb = args.vmverb
    elif verb in HOST_VERBS:
        host_verb = verb
    else:
        host_verb = None

    # The running inventory belongs to VMware, not a selected guest.
    if host_verb == "list":
        return cmd_vm_list(config, None, args)
    vm_name, vm = resolve_vm(config, args.vm)

    # Host-side vmrun verbs
    if host_verb is not None:
        if host_verb in HOST_HANDLERS:
            return HOST_HANDLERS[host_verb](config, vm, args)
        if host_verb == "reset":
            return cmd_vm_reset(config, vm, vm_name, args, config_path)
        if host_verb == "ip":
            return cmd_vm_ip(config, vm, vm_name, args, config_path)
        if host_verb == "doctor":
            return cmd_vm_doctor(config, vm, vm_name, args, config_path)
        if host_verb == "setup-ssh":
            return cmd_vm_setup_ssh(config, vm, vm_name, args)
        if host_verb == "provision":
            return cmd_vm_provision(config, vm, vm_name, args)
        die("unknown vm subcommand: %s" % host_verb)

    if verb == "mount":
        return cmd_mount(config, vm, args)
    if verb == "umount":
        return cmd_umount(config, vm, args)

    # Guest-side SSH verbs
    if verb in GUEST_HANDLERS:
        if guest_os(vm) == "windows" and (
            verb in {"build-run", "snap", "verify", "waitfile"}
            or getattr(args, "as_user", None)
        ):
            die(
                "%s%s is Linux-only"
                % (verb, " --as" if getattr(args, "as_user", None) else "")
            )
        client = ssh_connect(config, vm)
        try:
            if not args.no_provision:
                maybe_auto_provision(config, vm, vm_name, client)
            return GUEST_HANDLERS[verb](client, vm, args)
        finally:
            try:
                client.close()
            except Exception:
                pass

    die("unknown verb: %s" % verb)


def console_main():
    """Console-script entry point for the `vm` command."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(130)
    except TimeoutError as error:
        die(str(error), EXIT_TIMEOUT)
    except (OSError, ValueError, paramiko.SSHException) as error:
        die(str(error))


if __name__ == "__main__":
    console_main()
