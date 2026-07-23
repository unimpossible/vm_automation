# vm — cheat sheet

CLI to drive test VMs: guest control over SSH (paramiko) + host control over VMware (`vmrun`), primarily to be used by AI agents. 

One entrypoint: `vm [--vm NAME] <verb> ...`. Success = one status line; errors go to stderr
with a real message + propagated exit code. (Package `vm-automation-cli`, import `vm_cli`.)

## Setup
```
pip install .                               # installs the `vm` and `vm-init` commands
vm-init                                     # pick your VMs; writes config + folders
vm vm doctor                                # all checks should PASS
```
`vm-init` is interactive: it finds the VMs VMware knows about (running + registered), auto-detects
each guest's OS and IP, prompts for user/password/snapshot, and writes `vmconfig.json` plus the
`staging/` and `provision/` folders in the current directory. Re-run it any time to add more VMs
(it merges); pass `--agents` to also drop a "Test VM" section into `./AGENTS.md`. To fill in a
config by hand instead, `copy vmconfig.example.json vmconfig.json` and edit.

Config lookup: `--config PATH`, else `$VM_CONFIG`, else `vmconfig.json` in the working directory —
so `vm` finds the config (and `provision/`) in whatever project you run it from. `vmconfig.json` is
gitignored (holds passwords). `default_vm` is used when `--vm` is omitted. Running from source
without installing? Use `python -m vm_cli.cli <verb>` and `python -m vm_cli.init`. To wire the tool
into Claude Code or another coding agent, see `INSTALL.md`.

## Verbs

**Guest (SSH):**
| verb | args | purpose |
|---|---|---|
| `run` | `"<cmd>" [--as USER] [--timeout N]` | exec cmd, print stdout/stderr, exit=remote rc |
| `push` | `<src>... [dest]` | upload, cp-style (SFTP, auto base64 fallback) |
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
| `vm list` | | list running VMs |
| `vm snapshots` | | list snapshots |
| `vm ip` | `[--save]` | discover guest IP, optionally write to config |
| `vm doctor` | | health check: config, vmrun, vmx, SSH; per user: `--as` works + sudo rights match config's `sudo` flag |
| `vm setup-ssh` | | (Windows guest) enable OpenSSH Server over VMware Tools; idempotent |
| `vm provision` | `[--force]` | stage `provision/<vm\|os>/` into the guest tools dir; run its setup script |

**Optional (WSL):** `mount` / `umount` — sshfs live-bind the VM's `staging_remote` into WSL at
`$HOME/vmstaging_<host>` (under your WSL home, not `/mnt`, so no root/permissions needed).
`mount` first ensures `staging_remote` exists on the guest (sshfs can't create it), so it works
even before your first `push`/`sync`. Uses the VM's `wsl_distro` if set; otherwise falls back to
your **default** WSL distro (`vm-init` also auto-fills `wsl_distro` with it). Requires `sshfs` in
the distro (`sudo apt install sshfs`).

> **Note — accessing the mount from Windows.** The mount lives inside WSL. It can't be bound to your
> Windows-side `staging\` folder: WSL's FUSE refuses to mount over the `/mnt/<drive>` DrvFs bridge.
> Browsing it from Windows via `\\wsl$\<distro>\home\<user>\vmstaging_<host>` also doesn't work — the
> folder shows up but opening it returns *Access denied*, because the `\\wsl$` 9p bridge can't traverse
> a user-owned FUSE mount (a Windows symlink or `.lnk` shortcut inherits the same denial). Use the
> mount from **inside WSL**; for a Windows-native drive-letter mount, use `sshfs-win`/WinFsp instead.

## Provisioning (staging tools into the guest)

Drop files into `provision/<vm-name>/` (or `provision/<os>/`) in your project directory (beside
`vmconfig.json`). On the first guest command they're synced to the guest tools dir (`tools_remote`, default `<home>/tools`),
made executable on Linux, and that dir is prepended to `PATH` for `run` — so
`provision/myvm/strace` makes `run "strace -V"` work. No manifest; the folder is the config.

- **Setup hook:** an optional `setup.sh` (Linux) or `setup.ps1` (Windows) at the folder root runs
  once after the copy, for anything a plain copy can't do. `setup.ps1` runs **elevated** over the
  VMware Tools channel (where an MSVC/Build Tools installer belongs — SSH can't elevate).
- **Idempotent:** a hash marker (`<tools>/.provisioned`) skips unchanged folders; edit the folder
  and the next command auto-restages. Force with `vm provision --force`.
- **Bake it in:** provision once, then `vm snapshot clean`, so every `vm reset` restores a
  fully-loaded guest for free. See `provision/README.md`.

## Windows guests

Set `"os": "windows"` on the VM block (see `mywinvm` in `vmconfig.example.json`). Windows has no
SSH by default, so the first guest command needs OpenSSH turned on inside the VM. `vm` does this
for you over VMware Tools — no manual step:

- It happens **automatically** the first time an SSH verb can't connect (you'll see
  `enabling OpenSSH in the Windows guest...`), or run it explicitly with `vm setup-ssh`.
- Requirements: VMware Tools running in the guest, and `default_user` is a **local admin**
  (the modern.ie test VMs' `IEUser` / `Passw0rd!` qualifies).
- Use forward slashes in remote paths (`C:/Users/IEUser/staging`). `run` executes in `cmd.exe`.
- `push` / `pull` / `sync` / `run` work. `build-run`, `--as`, and sudo are Linux-only.

## Exit codes
- Remote command's rc passes through for `run` / `build-run`.
- `124` = timeout.
- `125` = can't connect / config error.
- `0` = success (other verbs).

## If a command fails, do this
| symptom | action |
|---|---|
| exit `125` (can't connect) | `vm vm ip --save` then retry once; still failing → `vm vm doctor` |
| exit `124` (timeout) | retry with a bigger `--timeout N`; if it repeats, the command is hanging — report it |
| nonzero rc from `run`/`build-run` | that is the remote command's own exit code — read the printed stderr |
| `vm doctor` shows a `[FAIL]` | fix that one line (config value, vmrun path, credentials); don't retry other verbs first |
| VM is broken / reverted | use the `vm-recovery` skill (or: `vm reset`, then re-`sync`) |

Do not retry the same failing command more than twice.

## No verb needed for a permission/read check
```
vm run "head -c 64 <path>" --as USER
```

## snap/verify are stateless
`snap` prints a baseline line only — nothing stored on disk. Pass it back via
`verify <path> --baseline "<line>"`. Safe for concurrent agents (no shared state file).

## Transferring multiple files
`push` is cp-style. One source uses a default remote; `SRC DEST` sets a literal remote path;
`SRC... DESTDIR` pushes many files into a remote directory (shell globs work):
```
vm push ./a.txt /home/user/a.txt          # single, explicit path
vm push ./a.c ./b.c ./data /home/user/in/  # many files -> a remote dir
```
For a whole tree, use `sync <localdir> [remotedir]` (recursive, defaults from config staging).

## build-run working dir
By default `build-run` builds in a fresh `/tmp/vmbuild.XXXXXX` dir (unique per run so concurrent
agents don't collide) and removes it afterward; `--keep` leaves it (path printed to stderr). Pass
`--dir REMOTE` to build into a chosen dir (created if needed) and leave the source + binary there —
the binary is named after the source stem (`widget.c` → `widget`). The program runs **with the
build dir as its cwd**, so relative paths it opens/creates land next to its artifacts.

## build-run --args
A single `--args` value is split shell-style: `--args "1 2 3"` passes **three** arguments.
Multiple values pass through literally, so `--args alpha "two words"` passes two arguments,
the second containing a space.

## waitfile deletes a pre-existing target
`waitfile <path>` means "wait until the watched job creates this file". If the file already
exists when waitfile starts (stale output from an earlier run), it is deleted first and then
watched for recreation — so start your job, then call waitfile, in either order.

## Git Bash / MSYS path gotcha
On Git Bash/MSYS, an absolute POSIX **remote** path in ANY verb that takes one — `push`, `pull`,
`snap`, `verify`, `waitfile`, `build-run --dir` — gets silently rewritten to a Windows path before
`vm` sees it (a `snap` will just print `MISSING` for a file that exists). **Local** paths are the
mirror image: use native Windows form (`C:\Users\...`), not `/c/Users/...`. Prefix the command
with `MSYS_NO_PATHCONV=1`, or just run from PowerShell (unaffected):
```
MSYS_NO_PATHCONV=1 vm push ./x /home/user/x
```

## Examples
```
vm run "id" --as admin
vm build-run ./test.c --args "1 2 3"
vm build-run ./test.c --dir /home/user/build   # leaves source + binary there
vm push ./out.bin /tmp/out.bin && vm run "wc -c /tmp/out.bin"
vm push ./a.c ./b.c /home/user/src/            # multiple files in one call
vm vm reset
```
