# vm.py — cheat sheet

CLI to drive test VMs: guest control over SSH (paramiko) + host control over VMware (`vmrun`), primarily to be used by AI agents. 

One entrypoint: `python vm.py [--vm NAME] <verb> ...`. Success = one status line; errors go to stderr
with a real message + propagated exit code.

## Setup
```
pip install -r requirements.txt             # just paramiko
copy vmconfig.example.json vmconfig.json    # edit: host IP, vmx path, passwords
python vm.py vm doctor                      # all checks should PASS
```
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

**Optional (WSL):** `mount` / `umount` — sshfs live-bind VM staging dir via named WSL distro.

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
