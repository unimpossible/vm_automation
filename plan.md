# Generic VM automation tool

## Context
A single, low-overhead command-line tool for driving test VMs from a Windows host — designed so an
AI agent can operate it with minimal token cost. It does two things:

1. **Guest control over SSH:** run commands, move files, and compile-and-run a local test program on
   the VM, returning results.
2. **Host control over VMware:** snapshot, revert, reset, power, and IP discovery via `vmrun`, so a
   VM that gets into a bad state can be restored to a clean snapshot in one command (and its new IP
   picked up automatically).

The tool is **fully generic** — no project- or domain-specific assumptions. It lives in its own new
folder (its own git repo), holds a registry of **named VMs** so you target a VM by a friendly name,
and keeps all connection details in one config file.

Design is distilled from earlier ad-hoc helpers whose pain points were: four near-duplicate SSH
scripts with hardcoded credentials, flaky SFTP that needed a base64 fallback, inline-shell quoting
that mangled multi-line commands, no host-side VM control (bad VM states required manual reinstall,
and the VM returned with a new IP that had to be edited everywhere), and poll-loops that wasted
output waiting for a file to appear.

## Location
New standalone folder, its own git repository (so it's reusable across projects and satisfies the
cloud-planning git requirement):
```
E:\Projects\generic_vm\
  vm.py                    # single CLI entrypoint: guest (SSH) + host (VMware) verbs
  vmconfig.json            # single source of truth: named-VM registry + per-VM details (GITIGNORED — holds passwords)
  vmconfig.example.json    # committed template with placeholder values (valid JSON, no comments)
  README.md                # ultra-terse cheat sheet the agent reads once
  SKILL.md                 # snapshot/reset skill
  .gitignore               # vmconfig.json, __pycache__, staging/
```

## Design principles (token frugality is the point)
- **One entrypoint, terse verbs:** `python vm.py [--vm NAME] <verb> ...`. Success prints one status
  line; full stdout only where it is the actual result. Errors go to stderr with the real message
  and a propagated exit code.
- **Named VMs:** config holds a `vms` map (name → connection details). `--vm NAME` selects one;
  a `default_vm` is used when omitted. `vmrun` operations resolve the name to that VM's `.vmx` path.
- **One config = one source of truth:** IP, `.vmx` path, `vmrun.exe` path, clean-snapshot name,
  per-user passwords, staging dirs, optional WSL distro — all per named VM. An IP change after a
  reset is a one-field edit, or auto-refreshed by `vm ip --save`.
- **Encode reusable lessons as verbs** so the agent stops re-deriving them: build-and-run,
  snapshot-a-file / verify-by-content, wait-for-file + who-wrote-it, run-as-another-user.
- **Generic core, guest-specific edges:** SSH and VMware layers are OS-agnostic; only compile
  (`gcc` vs `cl`) and the shell branch on a per-VM `os` field. v1 implements Linux; leaves clear
  hooks for a Windows guest.

Pure Python + `paramiko` + `subprocess`→`vmrun.exe`. No WSL required except the optional `mount`
verb.

## Robustness rules (agents feel these most)
- **Timeouts everywhere, fail fast:** paramiko connect ~10s; `run`/`build-run` default command
  timeout 120s (`--timeout` overrides); `vm reset`'s wait-for-SSH bounded. A dead VM must produce
  one clear stderr line in seconds, never a hang.
- **Distinct exit codes:** the remote command's rc passes through for `run`/`build-run`; reserved
  codes `124` = timeout, `125` = can't connect / config error — so the agent can tell "my test
  failed" from "the VM is down" without parsing text. Documented in README.
- **Host keys:** paramiko uses `AutoAddPolicy` — after `vm revert`/`reset` the host key and IP can
  both change, and every reset would otherwise break SSH. Fine for throwaway test VMs.
- **Concurrency-safe:** multiple agents may drive the same VM at once. No shared state files
  (`verify` takes its baseline as an argument); `build-run` uses `mktemp`-unique paths; the only
  config write (`vm ip --save` / `reset`) is atomic (temp + rename).

## `vm.py` verbs

**Guest — over SSH (paramiko, password from config):**
- `run "<cmd>" [--as USER]` — exec; prints stdout, stderr only if nonempty, exits with remote rc.
  `--as USER` runs as another account via piped `sudo -S` (password from config).
- `push <local> [remote]` — upload: try SFTP, **auto-fallback to base64-over-exec** if SFTP fails.
- `pull <remote> [local]` — download (SFTP + b64 fallback).
- `sync <localdir> [remotedir]` — bulk push a staging dir (defaults from the VM's config).
- `build-run <local-src> [--as USER] [--args ...]` — **the "create a test program locally, get
  results" primitive:** push the source, compile it *on the VM* (`gcc -O0 -o <tmp> <src>`,
  target-native), run it (optionally as another user), stream output + rc — one call. Non-compiled
  sources (`.sh`/`.py`) skip the compile step. Uses a unique temp name (`mktemp`) so concurrent
  agents on the same VM don't collide, and removes the binary afterwards (`--keep` to skip cleanup).

**Guest — result-verification helpers (stateless, concurrency-safe):**
- `snap <path>` — one-line `inode mtime size sha256` snapshot of a target before a run. Prints the
  baseline line; nothing is stored on disk — the caller keeps it.
- `verify <path> --baseline "<snap-line>" [--token STR]` — re-read; print
  `CREATED|MODIFIED|UNCHANGED` vs the passed-in baseline, and whether `--token` appears in content.
  Stateless by design: multiple agents running experiments concurrently each carry their own
  baseline; there is no shared state file to race on.
- `waitfile <path> [--timeout N]` — block until the file exists/changes. Replaces
  `for i in $(seq...); do sleep 3` poll loops.

(A permission/read check needs no verb: `run "head -c 64 <path>" --as USER` — noted in README.)

**Host — VMware control via `vmrun.exe`:**
- `vm snapshot [SNAP]` — take a snapshot (defaults to the VM's clean-snapshot name in config).
- `vm revert [SNAP]` — revert to a snapshot.
- `vm reset` — **one-command recovery:** revert to the clean snapshot → power on → wait for SSH →
  refresh IP in config.
- `vm start | stop | list | snapshots` — power + inventory.
- `vm ip [--save]` — discover guest IP (`vmrun getGuestIPAddress -wait`, with a retry loop since
  Tools isn't up immediately after boot), optionally write it back to `vmconfig.json` (handles the
  new-IP-after-reset case in one step; config writes are atomic — write temp + rename).
- `vm doctor` — one-shot setup/health check: config parses, `vmrun.exe` exists, `.vmx` path exists,
  SSH connects, sudo works for each configured `--as` user. Replaces five probing commands when
  something is broken.

**Optional (WSL):**
- `mount` / `umount` — sshfs live-bind the VM staging dir onto the host via a named WSL distro, so
  local edits appear on the VM with no re-upload. Off by default; only for users who want it.

## Config sketch (`vmconfig.json`)
```json
{
  "vmrun": "C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe",
  "default_vm": "target",
  "vms": {
    "target": {
      "os": "linux",
      "host": "192.168.187.130",
      "vmx": "",                       // fill in: E:\\Projects\\VMs\\<name>\\<name>.vmx
      "snapshot": "clean",
      "default_user": "user",
      "users": {
        "user":  {"password": "user", "sudo": true},
        "admin": {"password": "admin"}
      },
      "staging_local": ".\\staging",
      "staging_remote": "/home/user/staging",
      "wsl_distro": ""                 // optional, only for `mount`
    }
  }
}
```
`--vm target` (or the `default_vm`) selects the block. `vmrun` verbs use its `vmx`; SSH verbs use its
`host`/`users`.

## Reuse / grounding
- Paramiko password-auth + `exec_command` for `run`.
- Base64-over-exec upload as the SFTP fallback for `push`/`pull`.
- Run-as-another-user via piped `sudo -S`.
- Optional sshfs live-mount via a named WSL distro (idempotent, reconnect) for `mount`.
- `vmrun.exe` confirmed present at `C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe`.

## SKILL.md (snapshot/reset skill)
A short skill documenting the recovery loop for the agent: capture a known-good `vm snapshot clean`
once, then `vm reset` to restore + re-fetch IP after any risky operation, then re-`sync`/`mount`.
Turns the manual reinstall + IP-update dance into one command.

## Verification (end-to-end, once implemented)
1. `python vm.py --vm target run "id"` prints the expected uid; `run "id" --as admin` shows the other
   account.
2. `push README.md /tmp/x` then `run "wc -c /tmp/x"` — byte count matches; force an SFTP failure to
   confirm the base64 fallback triggers.
3. `build-run` a 5-line C program printing `getuid()` — compiles on the VM, runs, returns the uid;
   `--as admin` returns the other uid.
4. `snap /tmp/x` (capture the printed baseline), modify it on the VM, then
   `verify /tmp/x --baseline "<line>" --token foo` → `MODIFIED` with token presence reported
   correctly. Run twice in parallel shells to confirm no shared-state interference.
5. `vm doctor` reports all-green on the working setup; break the `vmx` path and confirm it pinpoints
   the failure. `run` against a stopped VM exits `125` with one stderr line in under ~15s.
6. Host control (after the user sets `vmx` and creates a `clean` snapshot): `vm snapshots` lists it;
   `vm ip --save` writes the current IP; `vm reset` reverts + powers on + reconnects.

## Automated tests (pytest)
Live integration tests in `tests/`, run against the real, already-running VM **`ubuntu24`**
(vmx: `E:\Users\Jonathan\Documents\Virtual Machines\ubuntu24\ubuntu24.vmx`, creds `user:user`).
Rules:
- **Guest-side writes are confined to `/home/user/Desktop/vm_automation_test/`** — each test session
  creates a unique subfolder there (`run-<uuid>/`) and removes it on teardown, so parallel runs
  don't collide and nothing else on the VM is touched.
- Host-side local artifacts use pytest's `tmp_path` only.
- **Read-only host verbs only** in tests: `vm list`, `vm snapshots`, `vm ip`. Never `revert`,
  `reset`, `stop`, or `snapshot` against the user's live VM — those would destroy its running state.
- Tests invoke the CLI as a subprocess (`python vm.py ...`) — they test the real interface the agent
  uses, including exit codes: remote rc passthrough, `124` timeout, `125` connect/config error.
- Coverage: `run` (incl. `--as` sudo path), `push`/`pull` (incl. forced base64 fallback), `sync`,
  `build-run` (C and `.sh`, `--args`, cleanup, `--keep`), `snap`/`verify` (CREATED/MODIFIED/
  UNCHANGED, `--token`, parallel-safety), `waitfile` (incl. timeout), `doctor`, exit codes,
  read-only host verbs.

## Out of scope (v1)
- Windows-guest compile/shell branches (hooks left, not implemented).
- WinFsp/SSHFS-Win drive-letter mount (WSL sshfs is the only mount path, and it's optional).
