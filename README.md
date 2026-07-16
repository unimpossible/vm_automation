# vm.py — cheat sheet

CLI to drive test VMs: guest control over SSH (paramiko) + host control over VMware (`vmrun`). One
entrypoint: `python vm.py [--vm NAME] <verb> ...`. Success = one status line; errors go to stderr
with a real message + propagated exit code.

## Setup
```
pip install paramiko
copy vmconfig.example.json vmconfig.json
# edit vmconfig.json: vmx path, host IP, snapshot name, per-user passwords
```
`vmconfig.json` is gitignored (holds passwords). `default_vm` in config is used when `--vm` is
omitted.

## Verbs

**Guest (SSH):**
| verb | args | purpose |
|---|---|---|
| `run` | `"<cmd>" [--as USER] [--timeout N]` | exec cmd, print stdout/stderr, exit=remote rc |
| `push` | `<local> [remote]` | upload (SFTP, auto base64 fallback) |
| `pull` | `<remote> [local]` | download (SFTP, auto base64 fallback) |
| `sync` | `<localdir> [remotedir]` | bulk push a staging dir |
| `build-run` | `<local-src> [--as USER] [--args ...] [--keep]` | push+compile(gcc)+run source in a temp dir, one call |
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
| `vm doctor` | | health check: config, vmrun, vmx path, SSH, sudo per user |

**Optional (WSL):** `mount` / `umount` — sshfs live-bind VM staging dir via named WSL distro.

## Exit codes
- Remote command's rc passes through for `run` / `build-run`.
- `124` = timeout.
- `125` = can't connect / config error.
- `0` = success (other verbs).

## No verb needed for a permission/read check
```
python vm.py run "head -c 64 <path>" --as USER
```

## snap/verify are stateless
`snap` prints a baseline line only — nothing stored on disk. Pass it back via
`verify <path> --baseline "<line>"`. Safe for concurrent agents (no shared state file).

## build-run working dir
`build-run` pushes the source and builds in a fresh `/tmp/vmbuild.XXXXXX` dir (unique per run, so
concurrent agents don't collide) and removes it afterward. Pass `--keep` to leave it in place (the
path is printed to stderr). It does not build into a caller-chosen directory — if the artifact must
live at a specific path, use `push` + `run "gcc ..."` instead.

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
python vm.py push ./out.bin /tmp/out.bin && python vm.py run "wc -c /tmp/out.bin"
python vm.py vm reset
```
