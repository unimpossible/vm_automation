#!/usr/bin/env python3
"""vm-init - interactive setup for the `vm` CLI.

Discovers the VMs VMware currently knows about (running ones via `vmrun list`,
registered ones from the inventory), lets you choose which to add, auto-detects
each guest's OS and IP, and writes a vmconfig.json plus the staging/ and
provision/ folder structure in the current directory. Optionally appends a
"Test VM" section to a local AGENTS.md. Re-runnable: merges into an existing config.

Usage:  vm-init [--config PATH] [--dir BASE] [--agents]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

VMRUN_CANDIDATES = [
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
]

PROVISION_README = """\
# provision/ - tools staged into the guest on first run

Drop files in `provision/<vm-name>/` (or `provision/<os>/`) in this directory. On
the first guest command they sync to the guest tools dir (`tools_remote`, default
`<home>/tools`), are made executable on Linux, and that dir is prepended to `PATH`
for `run`. No manifest - the folder is the config.

An optional `setup.sh` (Linux) / `setup.ps1` (Windows, run elevated over VMware
Tools) at the folder root runs once after the copy, for anything a plain copy
can't do (installers, apt, registry).

Run/refresh with `vm --vm NAME vm provision [--force]`.
"""

AGENTS_SECTION = """\
## Test VM

A local VM is available for running and testing code, driven by the `vm` CLI (on PATH).

- `vm run "<cmd>"` - run a command in the VM; exit code is the command's own
- `vm push <file>... <dest>` / `vm pull <remote> [local]` - copy files in/out
- `vm sync <dir>` - bulk-upload a directory
- `vm build-run <src>` - upload, compile, and run a source file (Linux)
- `vm vm doctor` - health check; `vm vm reset` - restore the clean snapshot

Config is `vmconfig.json` in this directory (created by `vm-init`). Tools placed in
`provision/<vm>/` are auto-installed into the guest on first use and added to PATH.

Rules: confine guest writes to an agreed directory; never run
`vm vm revert/reset/stop/snapshot` unless explicitly asked or the VM is broken.
If a command fails, run `vm vm doctor` before retrying; don't retry more than twice.
"""


# --- small prompt helpers ----------------------------------------------------
def ask(msg, default=None):
    suffix = " [%s]" % default if default not in (None, "") else ""
    try:
        val = input("%s%s: " % (msg, suffix)).strip()
    except EOFError:
        val = ""
    return val or (default or "")


def ask_yes(msg, default=False):
    d = "Y/n" if default else "y/N"
    try:
        val = input("%s [%s]: " % (msg, d)).strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes")


def ask_secret(msg):
    if sys.stdin.isatty():
        import getpass
        try:
            return getpass.getpass(msg + ": ")
        except (EOFError, KeyboardInterrupt):
            return ""
    # piped / non-tty: fall back to a normal read so scripted runs work
    try:
        return input(msg + ": ")
    except EOFError:
        return ""


# --- color + interactive multiselect -----------------------------------------
def _enable_vt():
    """Turn on ANSI escape processing in the Windows console (no-op elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


class Color:
    def __init__(self, on):
        self.on = on

    def _w(self, s, code):
        return "\x1b[%sm%s\x1b[0m" % (code, s) if self.on else s

    def green(self, s):
        return self._w(s, "32")

    def cyan(self, s):
        return self._w(s, "36;1")

    def bold(self, s):
        return self._w(s, "1")

    def dim(self, s):
        return self._w(s, "2")


def color_enabled():
    return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


def _norm_key(ch):
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    if ch in (b"\x03", b"\x1b", b"q", b"Q"):
        return "quit"
    if ch in (b"a", b"A"):
        return "all"
    if ch in (b"k", b"K"):
        return "up"
    if ch in (b"j", b"J"):
        return "down"
    return ""


def read_key():
    """Read one keystroke and normalize to up/down/space/enter/all/quit/''."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            return {b"H": "up", b"P": "down"}.get(msvcrt.getch(), "")
        return _norm_key(ch)
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.buffer.read(1)
        if ch == b"\x1b":
            seq = sys.stdin.buffer.read(2)
            return {b"[A": "up", b"[B": "down"}.get(seq, "")
        return _norm_key(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def multiselect(labels, preselected, color, key_source=None):
    """Interactive checkbox list. Returns sorted list of selected indices, or None
    if cancelled. `key_source` (an iterable of key tokens) makes it testable."""
    sel = set(preselected)
    cur = 0
    n = len(labels)
    instr = "up/dn move   space select   a all/none   enter confirm   q cancel"
    header = 2  # instruction line + blank

    def draw(first):
        if not first:
            sys.stdout.write("\x1b[%dA" % (n + header))
        sys.stdout.write("\r\x1b[0J")
        sys.stdout.write("  " + color.dim(instr) + "\n\n")
        for i, lab in enumerate(labels):
            box = color.green("[x]") if i in sel else "[ ]"
            if i == cur:
                sys.stdout.write("%s %s %s\n" % (color.cyan(">"), box, color.bold(lab)))
            else:
                sys.stdout.write("  %s %s\n" % (box, lab))
        sys.stdout.flush()

    keys = iter(key_source) if key_source is not None else None

    def nextkey():
        if keys is not None:
            return next(keys)
        try:
            return read_key()
        except KeyboardInterrupt:
            return "quit"

    draw(True)
    while True:
        try:
            k = nextkey()
        except StopIteration:
            return None
        if k == "up":
            cur = (cur - 1) % n
        elif k == "down":
            cur = (cur + 1) % n
        elif k == "space":
            sel.symmetric_difference_update({cur})
        elif k == "all":
            sel = set() if len(sel) == n else set(range(n))
        elif k == "enter":
            break
        elif k == "quit":
            return None
        draw(False)
    return sorted(sel)


# --- VMware discovery --------------------------------------------------------
def find_vmrun():
    for p in VMRUN_CANDIDATES:
        if os.path.isfile(p):
            return p
    from shutil import which
    w = which("vmrun")
    if w:
        return w
    return None


def vmrun_list_running(vmrun):
    """Return a set of vmx paths for currently running VMs."""
    try:
        p = subprocess.run([vmrun, "list"], capture_output=True, text=True, timeout=30)
    except Exception:
        return set()
    out = set()
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.lower().endswith(".vmx"):
            out.add(os.path.normpath(line))
    return out


def read_inventory():
    """Best-effort: vmx paths registered in the VMware inventory (all, not just running)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return set()
    inv = os.path.join(appdata, "VMware", "inventory.vmls")
    if not os.path.isfile(inv):
        return set()
    try:
        with open(inv, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return set()
    paths = set()
    for m in re.finditer(r'\.config\s*=\s*"([^"]+\.vmx)"', text):
        paths.add(os.path.normpath(m.group(1)))
    return paths


def read_vmx_meta(vmx):
    """Pull displayName and guestOS out of a .vmx file (best-effort)."""
    meta = {}
    try:
        with open(vmx, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().lower()
                v = v.strip().strip('"')
                if k in ("displayname", "guestos"):
                    meta[k] = v
    except OSError:
        pass
    return meta


def guess_os(guestos):
    return "windows" if "win" in (guestos or "").lower() else "linux"


def get_ip(vmrun, vmx, timeout=25):
    try:
        p = subprocess.run([vmrun, "getGuestIPAddress", vmx, "-wait"],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    ip = (p.stdout or "").strip()
    if p.returncode == 0 and ip and ip[0].isdigit():
        return ip
    return None


# --- config assembly ---------------------------------------------------------
def default_name(vmx, meta):
    return meta.get("displayname") or os.path.splitext(os.path.basename(vmx))[0]


def os_defaults(os_type, user):
    if os_type == "windows":
        home = "C:/Users/%s" % user
        return {"staging_remote": home + "/staging", "tools_remote": home + "/tools"}
    home = "/root" if user == "root" else "/home/%s" % user
    return {"staging_remote": home + "/staging", "tools_remote": home + "/tools"}


def collect_vm(vmrun, vmx, meta, running):
    """Interactively gather one VM's config block. Returns (name, block)."""
    name = ask("  VM name", default_name(vmx, meta))
    os_type = ask("  OS (linux/windows)", guess_os(meta.get("guestos"))).lower()
    if os_type not in ("linux", "windows"):
        os_type = "linux"

    host = ""
    if vmx in running:
        print("  detecting guest IP...")
        host = get_ip(vmrun, vmx) or ""
        if not host:
            print("  (could not detect IP; leaving blank - use `vm ip --save` later)")
    else:
        print("  (VM not running; leaving host blank - use `vm ip --save` later)")
    host = ask("  host IP", host)

    default_user = ask("  default_user",
                       "IEUser" if os_type == "windows" else "user")
    password = ask_secret("  password for %s" % default_user)
    snapshot = ask("  clean snapshot name", "clean")

    d = os_defaults(os_type, default_user)
    users = {default_user: {"password": password}}
    if os_type != "windows":
        users[default_user]["sudo"] = True

    block = {
        "os": os_type,
        "host": host,
        "vmx": vmx,
        "snapshot": snapshot,
        "default_user": default_user,
        "users": users,
        "staging_local": ".\\staging",
        "staging_remote": d["staging_remote"],
        "tools_remote": d["tools_remote"],
        "wsl_distro": "",
    }
    return name, block


def atomic_write_json(path, data):
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


def ensure_folders(base, vm_names):
    made = []
    staging = os.path.join(base, "staging")
    if not os.path.isdir(staging):
        os.makedirs(staging, exist_ok=True)
        made.append(staging)
    prov = os.path.join(base, "provision")
    os.makedirs(prov, exist_ok=True)
    readme = os.path.join(prov, "README.md")
    if not os.path.isfile(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(PROVISION_README)
        made.append(readme)
    for n in vm_names:
        d = os.path.join(prov, n)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            made.append(d)
    return made


def write_agents(base, interactive, force):
    """Append the 'Test VM' section to <base>/AGENTS.md. Idempotent (skips if the
    section is already there). Returns the path if written, else None."""
    path = os.path.join(base, "AGENTS.md")
    cur = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cur = f.read()
        except OSError:
            cur = ""
        if "## Test VM" in cur:
            return None  # already present
    if not force:
        if not interactive or not ask_yes("Append a 'Test VM' section to ./AGENTS.md?",
                                          default=False):
            return None
    sep = "" if not cur else ("\n" if cur.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(sep + AGENTS_SECTION.strip() + "\n")
    return path


# --- main --------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="vm-init",
                                 description="Interactive setup for the vm CLI")
    ap.add_argument("--config", default=os.path.join(os.getcwd(), "vmconfig.json"),
                    help="config path to write (default: ./vmconfig.json)")
    ap.add_argument("--dir", default=os.getcwd(),
                    help="base dir for staging/ and provision/ (default: current directory)")
    ap.add_argument("--agents", action="store_true",
                    help="append a 'Test VM' section to ./AGENTS.md without prompting")
    args = ap.parse_args(argv)

    vmrun = find_vmrun()
    if not vmrun:
        vmrun = ask("Path to vmrun.exe")
        if not vmrun or not os.path.isfile(vmrun):
            print("error: vmrun.exe not found; cannot continue.", file=sys.stderr)
            return 1
    print("Using vmrun: %s" % vmrun)

    running = vmrun_list_running(vmrun)
    available = set(running) | read_inventory()
    if not available:
        print("No VMs found (none running and no inventory). Start a VM and re-run.",
              file=sys.stderr)
        return 1

    # existing config -> merge into it
    cfg = {"vmrun": vmrun, "default_vm": None, "vms": {}}
    if os.path.isfile(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("vms", {})
            cfg["vmrun"] = vmrun
            print("Merging into existing config: %s (%d VM(s) already there)"
                  % (args.config, len(cfg["vms"])))
        except (OSError, ValueError):
            print("warning: could not read existing config; starting fresh.")

    ordered = sorted(available, key=lambda p: default_name(p, read_vmx_meta(p)).lower())
    entries = [(vmx, read_vmx_meta(vmx)) for vmx in ordered]
    labels = []
    for vmx, meta in entries:
        tag = "running" if vmx in running else "registered"
        labels.append("%-26s %-12s %s" % (default_name(vmx, meta), "[%s]" % tag, vmx))
    preselected = [i for i, (vmx, _m) in enumerate(entries) if vmx in running]

    color = Color(color_enabled())
    if color.on:
        _enable_vt()
    interactive = bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())

    print("\nFound %d VM(s). Choose which to add:\n" % len(entries))
    if interactive:
        chosen = multiselect(labels, preselected, color)
        if chosen is None:
            print("\nCancelled; nothing written.")
            return 0
    else:
        chosen = []
        for i, (vmx, meta) in enumerate(entries):
            tag = "running" if vmx in running else "registered"
            print("- %s [%s] (%s)" % (default_name(vmx, meta), tag, vmx))
            if ask_yes("  Add this VM?", default=(vmx in running)):
                chosen.append(i)

    added = []
    for i in chosen:
        vmx, meta = entries[i]
        print("\nConfiguring %s:" % default_name(vmx, meta))
        name, block = collect_vm(vmrun, vmx, meta, running)
        cfg["vms"][name] = block
        added.append(name)

    if not cfg["vms"]:
        print("\nNo VMs selected; nothing written.")
        return 0

    # default_vm
    names = sorted(cfg["vms"])
    cur = cfg.get("default_vm")
    if cur not in cfg["vms"]:
        cur = added[0] if added else names[0]
    if len(names) > 1:
        cur = ask("\ndefault VM (used when --vm is omitted)", cur)
        if cur not in cfg["vms"]:
            cur = names[0]
    cfg["default_vm"] = cur

    if os.path.isfile(args.config):
        if not ask_yes("\nOverwrite %s with the merged config?" % args.config, default=True):
            print("Aborted; config not written.")
            return 0
    atomic_write_json(args.config, cfg)
    made = ensure_folders(args.dir, list(cfg["vms"]))
    print("\nWrote %s" % args.config)

    agents = write_agents(args.dir, interactive, args.agents)

    print("  VMs:       %s" % ", ".join(names))
    print("  default:   %s" % cfg["default_vm"])
    if made:
        print("  created:   %s" % ", ".join(os.path.relpath(m, args.dir) for m in made))
    if agents:
        print("  AGENTS.md: appended 'Test VM' section")
    print("\nNext: vm vm doctor")
    return 0


def console_main():
    """Console-script entry point for the `vm-init` command."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)


if __name__ == "__main__":
    console_main()
