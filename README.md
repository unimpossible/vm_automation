# vm - an automation cli

CLI to drive test VMs — guest control over SSH (paramiko) + host control over VMware (`vmrun`) —
built for AI agents, so an LLM can drive a VM repeatably for product testing.

One entrypoint: `vm [--vm NAME] <verb> ...`. Success = one status line; errors go to stderr with a
real message and a propagated exit code. (Package `vm-automation-cli`, import `vm_cli`.)

## Setup
```
pip install .                               # installs the `vm` and `vm-init` commands
vm-init                                     # pick your VMs; writes config + folders
vm vm doctor                                # all checks should PASS
```
`vm-init` finds the VMs VMware knows about, auto-detects each guest's OS and IP, prompts for
user/password/snapshot, and writes `vmconfig.json` plus `staging/` and `provision/` here. Re-run it
to add VMs (it merges); `--agents` also drops a "Test VM" section into `./AGENTS.md`. To configure
by hand, copy `vmconfig.example.json` instead. From source, without installing:
`python -m vm_cli.cli <verb>` / `python -m vm_cli.init`.

Config lookup: `--config PATH`, else `$VM_CONFIG`, else `vmconfig.json` in the working directory —
so `vm` finds the config (and `provision/`) in whatever project you run it from. It holds passwords
and is gitignored. `default_vm` is used when `--vm` is omitted.

To wire this into Claude Code or another coding agent, see `INSTALL.md`.

## Verbs

**Guest (SSH):**
| verb | args | purpose |
|---|---|---|
| `run` | `"<cmd>" [--as USER] [--timeout N]` | exec cmd, print stdout/stderr, exit=remote rc |
| `push` | `<src>... [dest]` | upload files/dirs/globs, keeping relative paths (SFTP, auto base64 fallback) |
| `pull` | `<remote> [local]` | download (SFTP, auto base64 fallback) |
| `sync` | `<localdir> [remotedir]` | bulk push a staging dir |
| `build-run` | `<local-src> [--as USER] [--dir REMOTE] [--keep] [--args ...]` | push+compile(gcc)+run source, one call |
| `snap` | `<path>` | print baseline line `inode mtime size sha256` |
| `verify` | `<path> --baseline "<line>" [--token STR]` | print CREATED\|MODIFIED\|UNCHANGED + token check |
| `waitfile` | `<path> [--timeout N]` | block until file appears (pre-existing file is deleted first, then watched for recreation) |

**Host (vmrun):**
| verb | args | purpose |
|---|---|---|
| `vm snapshot` | `[SNAP]` | take snapshot (default: config's clean name) |
| `vm revert` | `[SNAP]` | revert to snapshot |
| `vm reset` | | revert to clean + power on + wait for SSH + refresh IP in config |
| `vm start` / `vm stop` | | power on/off |
| `vm list` / `vm snapshots` | | list running VMs / this VM's snapshots |
| `vm ip` | `[--save]` | discover guest IP, optionally write to config |
| `vm doctor` | | health check: config, vmrun, vmx, SSH; per user: `--as` works + sudo rights match config's `sudo` flag |
| `vm setup-ssh` | | (Windows guest) enable OpenSSH Server over VMware Tools; idempotent |
| `vm provision` | `[--force]` | stage `provision/<vm\|os>/` into the guest tools dir; run its setup script |

**Docs (needs no config):** `vm docs` prints this README from anywhere — it ships inside the
package, so an agent can read it with no repo checked out. `--skill` prints the `vm-recovery`
skill, `--install-skill [DIR]` writes it to `DIR/.claude/skills/vm-recovery/`, `--path` prints
the file's location.

**Optional (WSL):** `mount` / `umount` sshfs live-bind the VM's `staging_remote` into WSL at
`$HOME/vmstaging_<host>` (under your WSL home, not `/mnt`, so no root needed). `mount` creates
`staging_remote` on the guest first, so it works before your first `push`. Uses the VM's
`wsl_distro`, else your default distro; needs `sshfs` there. The mount is usable **only from inside
WSL** — it can't be bound to your Windows-side `staging\` (FUSE won't mount over DrvFs) and
`\\wsl$\...` returns *Access denied* on it. For a Windows drive letter, use `sshfs-win`/WinFsp.

## Provisioning (staging tools into the guest)

Drop files into `provision/<vm-name>/` (or `provision/<os>/`) beside `vmconfig.json`. On the first
guest command they sync to the guest tools dir (`tools_remote`, default `<home>/tools`), are made
executable on Linux, and that dir is prepended to `PATH` for `run` — so `provision/myvm/strace`
makes `run "strace -V"` work. No manifest; the folder is the config. See `provision/README.md`.

- **Setup hook:** an optional `setup.sh` / `setup.ps1` at the folder root runs once after the copy.
  `setup.ps1` runs **elevated** over VMware Tools — where an MSVC/Build Tools installer belongs,
  since SSH can't elevate.
- **Idempotent:** a hash marker (`<tools>/.provisioned`) skips unchanged folders; edit the folder
  and the next command restages. Force with `vm provision --force`.
- **Bake it in:** provision once, then `vm snapshot clean`, so every `vm reset` restores a
  fully-loaded guest for free.

## Windows guests

Set `"os": "windows"` on the VM block (see `mywinvm` in `vmconfig.example.json`). Windows has no
SSH by default; `vm` enables it over VMware Tools automatically the first time an SSH verb can't
connect (`enabling OpenSSH in the Windows guest...`), or on demand via `vm setup-ssh`. Needs VMware
Tools running and a **local admin** `default_user` (the modern.ie VMs' `IEUser` / `Passw0rd!`
qualifies). Use forward slashes in remote paths (`C:/Users/IEUser/staging`); `run` executes in
`cmd.exe`. `push` / `pull` / `sync` / `run` work — `build-run`, `--as`, and sudo are Linux-only.

### Windows 11 (encrypted / vTPM) guests

Windows 11 requires a vTPM, which VMware only provides on an **encrypted** VM — and `vmrun`
can't open an encrypted VM (to read its IP, run guest ops, etc.) without the encryption password.
Put it in the VM block:

```json
"encryption_password": "your-vm-encryption-password"
```

`vm-init` detects encryption and prompts for it automatically. With it set, the normal flow works:

```
vm-init                          # detects the VM, asks for the encryption password
vm --vm <NAME> vm setup-ssh      # installs OpenSSH + opens the firewall on all profiles
vm --vm <NAME> vm doctor         # verify
```

Two Win11-specific gotchas `vm` now handles for you:

- **First-boot VMware Tools:** a freshly-created guest often can't *launch* programs over Tools
  until it has rebooted once (auth and file ops work, process launch silently no-ops). `setup-ssh`
  detects this and fails fast telling you to reboot, instead of hanging. Reboot the guest once, then
  re-run.
- **Firewall profile:** VMware's NAT network is usually classified *Public*, and OpenSSH's default
  rule only allows Private/Domain — so port 22 stays blocked. `setup-ssh` forces the inbound rule to
  apply to all profiles.

## Exit codes
Remote command's rc passes through for `run` / `build-run`; `124` = timeout;
`125` = can't connect / config error; `0` = success (other verbs).

## If a command fails, do this
| symptom | action |
|---|---|
| exit `125` (can't connect) | `vm vm ip --save` then retry once; still failing → `vm vm doctor` |
| exit `124` (timeout) | retry with a bigger `--timeout N`; if it repeats, the command is hanging — report it |
| nonzero rc from `run`/`build-run` | that is the remote command's own exit code — read the printed stderr |
| `vm doctor` shows a `[FAIL]` | fix that one line (config value, vmrun path, credentials); don't retry other verbs first |
| VM is broken / reverted | use the `vm-recovery` skill (or: `vm reset`, then re-`sync`) |

Do not retry the same failing command more than twice.

## Transferring files (`push`)
`push` takes files, directories, and globs — **expanded by the tool itself**, so `push docs/*.txt`
works whether or not your shell expands it (PowerShell/cmd don't). Each source keeps its **relative
path under the destination** (default: `staging_remote`), so a `docs/` prefix is recreated:
```
vm push report.txt                 # -> <staging>/report.txt
vm push docs/*.txt                 # -> <staging>/docs/*.txt   (docs/ created; .txt only)
vm push src/**/*.py                # recursive glob
vm push docs                       # -> <staging>/docs/...     (whole dir, recursive)
vm push a.txt b.txt /home/user/in  # 2+ args: trailing non-local arg is an explicit remote dir
vm push local.txt /home/user/x.txt # single file + explicit path = literal rename
```
Absolute / drive-qualified / `..` sources fall back to their basename, so no host layout leaks into
the destination. A non-matching glob or missing file is an error (exit 125).

## build-run
Builds in a fresh `/tmp/vmbuild.XXXXXX` (unique per run, so concurrent agents don't collide) and
removes it afterward; `--keep` leaves it (path printed to stderr). `--dir REMOTE` builds into a
chosen dir and leaves source + binary there, named after the source stem (`widget.c` → `widget`).
The program runs **with the build dir as its cwd**. A single `--args` value is split shell-style
(`--args "1 2 3"` = three arguments); multiple values pass through literally.

## Other behaviors worth knowing
- **`waitfile` deletes a pre-existing target.** It means "wait until the job creates this file", so
  a stale file from an earlier run is deleted first, then watched for recreation — start your job
  and call waitfile in either order.
- **`snap`/`verify` are stateless.** `snap` only prints a baseline line; pass it back via
  `verify <path> --baseline "<line>"`. No shared state file, so concurrent agents are safe.
- **Git Bash / MSYS path gotcha.** An absolute POSIX **remote** path in any verb that takes one
  (`push`, `pull`, `snap`, `verify`, `waitfile`, `build-run --dir`) is silently rewritten to a
  Windows path before `vm` sees it — `snap` then prints `MISSING` for a file that exists. **Local**
  paths are the mirror image: use `C:\Users\...`, not `/c/Users/...`. Prefix with
  `MSYS_NO_PATHCONV=1`, or use PowerShell (unaffected).

## Examples
```
vm run "id" --as admin
vm run "head -c 64 /etc/shadow" --as admin      # permission/read check; no special verb needed
vm build-run ./test.c --args "1 2 3"
vm build-run ./test.c --dir /home/user/build    # leaves source + binary there
vm push ./out.bin /tmp/out.bin && vm run "wc -c /tmp/out.bin"
vm push ./a.c ./b.c /home/user/src/             # multiple files in one call
vm vm reset
```
