# vm.py — cheat sheet

CLI to drive test VMs: guest control over SSH (paramiko) + host control over VMware (`vmrun`), primarily to be used by AI agents. 

One entrypoint: `python vm.py [--vm NAME] <verb> ...`. Success = one status line; errors go to stderr
with a real message + propagated exit code.

## Setup
```
pip install -r requirements.txt             # just paramiko
python init.py                              # pick your VMs; writes config + folders
python vm.py vm doctor                      # all checks should PASS
```
`init.py` is interactive: it finds the VMs VMware knows about (running + registered), auto-detects
each guest's OS and IP, prompts for user/password/snapshot, and writes `vmconfig.json` plus the
`staging/` and `provision/` folders. Re-run it any time to add more VMs (it merges). To fill in a
config by hand instead, `copy vmconfig.example.json vmconfig.json` and edit.

`vmconfig.json` is gitignored (holds passwords). `default_vm` in config is used when `--vm` is
omitted. To wire the tool into Claude Code or another coding agent, see `INSTALL.md`.

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
| `waitfile` | `<path> [--timeout N]` | block until file exists/changes |

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

**Optional (WSL):** `mount` / `umount` — sshfs live-bind VM staging dir via named WSL distro.

## Provisioning (staging tools into the guest)

Drop files into `provision/<vm-name>/` (or `provision/<os>/`) next to `vm.py`. On the first
guest command they're synced to the guest tools dir (`tools_remote`, default `<home>/tools`),
made executable on Linux, and that dir is prepended to `PATH` for `run` — so
`provision/ubuntu24/strace` makes `run "strace -V"` work. No manifest; the folder is the config.

- **Setup hook:** an optional `setup.sh` (Linux) or `setup.ps1` (Windows) at the folder root runs
  once after the copy, for anything a plain copy can't do. `setup.ps1` runs **elevated** over the
  VMware Tools channel (where an MSVC/Build Tools installer belongs — SSH can't elevate).
- **Idempotent:** a hash marker (`<tools>/.provisioned`) skips unchanged folders; edit the folder
  and the next command auto-restages. Force with `vm provision --force`.
- **Bake it in:** provision once, then `vm snapshot clean`, so every `vm reset` restores a
  fully-loaded guest for free. See `provision/README.md`.

## Windows guests

Set `"os": "windows"` on the VM block (see `mywinvm` in `vmconfig.example.json`). Windows has no
SSH by default, so the first guest command needs OpenSSH turned on inside the VM. vm.py does this
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
| exit `125` (can't connect) | `python vm.py vm ip --save` then retry once; still failing → `python vm.py vm doctor` |
| exit `124` (timeout) | retry with a bigger `--timeout N`; if it repeats, the command is hanging — report it |
| nonzero rc from `run`/`build-run` | that is the remote command's own exit code — read the printed stderr |
| `vm doctor` shows a `[FAIL]` | fix that one line (config value, vmrun path, credentials); don't retry other verbs first |
| VM is broken / reverted | use the `vm-recovery` skill (or: `vm reset`, then re-`sync`) |

Do not retry the same failing command more than twice.

## No verb needed for a permission/read check
```
python vm.py run "head -c 64 <path>" --as USER
```

## snap/verify are stateless
`snap` prints a baseline line only — nothing stored on disk. Pass it back via
`verify <path> --baseline "<line>"`. Safe for concurrent agents (no shared state file).

## Transferring multiple files
`push` is cp-style. One source uses a default remote; `SRC DEST` sets a literal remote path;
`SRC... DESTDIR` pushes many files into a remote directory (shell globs work):
```
python vm.py push ./a.txt /home/user/a.txt          # single, explicit path
python vm.py push ./a.c ./b.c ./data /home/user/in/  # many files -> a remote dir
```
For a whole tree, use `sync <localdir> [remotedir]` (recursive, defaults from config staging).

## build-run working dir
By default `build-run` builds in a fresh `/tmp/vmbuild.XXXXXX` dir (unique per run so concurrent
agents don't collide) and removes it afterward; `--keep` leaves it (path printed to stderr). Pass
`--dir REMOTE` to build into a chosen dir (created if needed) and leave the source + binary there —
the binary is named after the source stem (`widget.c` → `widget`).

## Git Bash / MSYS path gotcha
On Git Bash/MSYS, an absolute POSIX **remote** path in `push`/`pull` (e.g. `/home/user/x`) gets
silently rewritten to a Windows path before `vm.py` sees it — the upload then "succeeds" at the wrong
place. Prefix the command with `MSYS_NO_PATHCONV=1`, or just run from PowerShell (unaffected):
```
MSYS_NO_PATHCONV=1 python vm.py push ./x /home/user/x
```

## Examples
```
python vm.py run "id" --as admin
python vm.py build-run ./test.c --args "1 2 3"
python vm.py build-run ./test.c --dir /home/user/build   # leaves source + binary there
python vm.py push ./out.bin /tmp/out.bin && python vm.py run "wc -c /tmp/out.bin"
python vm.py push ./a.c ./b.c /home/user/src/            # multiple files in one call
python vm.py vm reset
```
