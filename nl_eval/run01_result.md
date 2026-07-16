# NL usage eval — run 01 RESULT

**Model:** Sonnet · **Date:** 2026-07-15 · **Outcome:** all 5 steps succeeded.
(Prompt in `run01_prompt.md`. Agent had only the README + the task; it drove `vm.py` from Git Bash.)

## Numbered report (as returned by the agent)

**1. Health check** — `python vm.py vm doctor` → all 5 checks PASS. Success.

**2. User / distro / kernel** — `python vm.py run "whoami && id && cat /etc/os-release && uname -a"`
→ user `user` (uid 1000, in `sudo`), Ubuntu 24.04.4 LTS "Noble", kernel 6.11.0-25-generic x86_64. Success.

**3. Build-run C on VM** — wrote `uid_pagesize.c` (prints `getuid()` + `sysconf(_SC_PAGESIZE)`).
- `python vm.py build-run "...\uid_pagesize.c" --keep` → `uid=1000 / page_size=4096`, but built under
  `/tmp/vmbuild.XXXXXX`, NOT under the required Desktop path → discarded + cleaned up.
- Fell back to manual: `push` the source, then `run "cd .../vm_automation_test && gcc -o uid_pagesize uid_pagesize.c && ./uid_pagesize"`
  → `uid=1000 / page_size=4096`. Success (correct location on 2nd try).

**4. File integrity** — local SHA-256 `6861a821...0821d`; `push` to the Desktop path, then
`run "sha256sum ... && wc -c ..."` → identical hash, 143 bytes. Success (byte-for-byte).

**5. IP** — `python vm.py vm ip` → `192.168.187.130`. Success.

## Friction notes (verbatim, the valuable part)

1. **Git Bash/MSYS mangles POSIX remote paths on `push`/`pull`.** From Git Bash,
   `push local.c /home/user/Desktop/vm_automation_test/uid_pagesize.c` was silently rewritten by MSYS to
   `C:/Program Files/Git/home/user/Desktop/...`; vm.py faithfully created that (mangled) path over SFTP and
   reported success, so the file "arrived" — just not where intended. Only caught via `ls`. Fix: README note to
   prefix `MSYS_NO_PATHCONV=1` on Git Bash. (PowerShell — what the pytest suite uses — is unaffected.)

2. **`build-run` gives no control over / visibility into the remote working dir.** It builds in an
   auto-generated `/tmp/vmbuild.XXXXXX` (only revealed by `--keep`); the README example doesn't say where things
   land, and there's no `--dir` flag. For "output must live at path X" tasks the agent had to fall back to
   manual push + run gcc. Fix: document the `/tmp/vmbuild.*` behavior; consider an optional `--dir <remote>` flag.

3. **Minor:** success/failure of `push`/`run` is signaled only via printed text, so a wrong-but-successful
   placement (note 1) doesn't stand out. Not a blocker.

## Actions taken from this run
- README: added a Git Bash/MSYS `MSYS_NO_PATHCONV=1` warning for remote paths on `push`/`pull`.
- README: documented `build-run`'s `/tmp/vmbuild.XXXXXX` working dir and `--keep`.
- `--dir <remote>` for `build-run`: left as a recommendation for the owner to decide (scope addition).
