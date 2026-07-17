# Install

## Quick start

```
pip install -r requirements.txt              # just paramiko (add pytest to run tests/)
python init.py                               # interactive: pick VMs, writes config + folders
python vm.py vm doctor                       # checks: config, vmrun, vmx, SSH, sudo
```

`init.py` discovers the VMs VMware knows about, auto-detects OS + IP, prompts for the rest, and
creates `vmconfig.json` plus `staging/` and `provision/`. Prefer to hand-edit? Instead run
`copy vmconfig.example.json vmconfig.json` (`cp` on *nix) and fill it in.

When all doctor checks say `[PASS]`, you're done — `README.md` is the usage reference.
`vmconfig.json` holds passwords and is gitignored; never commit it. (Running `vm.py` without a
config prints these same setup steps.)

## Wiring it into Claude Code

Three optional steps, each independent:

**1. Tell the agent the tool exists** — add to your project's `CLAUDE.md`:
```md
## Test VM
Drive the test VM with `python vm.py <verb>` (run from E:\Projects\vm_automation).
Read its README.md once for the verbs. Rules: confine guest writes to an agreed dir;
never run `vm revert/reset/stop/snapshot` unless explicitly asked or your team wrecked the state.
```

**2. Skip permission prompts** — in `.claude/settings.json` (project) or `~/.claude/settings.json`:
```json
{
  "permissions": {
    "allow": ["Bash(python vm.py:*)", "PowerShell(python vm.py:*)"]
  }
}
```
To keep destructive host verbs behind a prompt, allowlist only safe ones instead
(`Bash(python vm.py run:*)`, `push:*`, `pull:*`, `build-run:*`, `vm doctor`, `vm ip:*`).

**3. Install the recovery skill** — makes `/vm-recovery` available (snapshot → reset → sync loop):
```
# PowerShell / cmd:
mkdir .claude\skills\vm-recovery
copy SKILL.md .claude\skills\vm-recovery\SKILL.md

# bash:
mkdir -p .claude/skills/vm-recovery && cp SKILL.md .claude/skills/vm-recovery/SKILL.md
```
Use `~/.claude/skills/...` instead for a global install.

## Other agents (Cursor, Aider, custom)

It's just a CLI — same three ideas: point the agent at `README.md`, allow `python vm.py ...` in
the harness's command allowlist, and state the guardrails below in the system prompt.

## Small models (Haiku-class)

A small model may not reliably open and digest `README.md` on its own. Instead of the pointer,
paste this self-contained snippet into its system prompt / CLAUDE.md — it needs no other reading:

```md
## Test VM — exact commands (run from E:\Projects\vm_automation)
python vm.py run "<cmd>"                      # run on VM; exit code = the command's own rc
python vm.py push <file>... /remote/dir/      # upload file(s)
python vm.py pull /remote/file [local]        # download
python vm.py build-run <src.c|.sh|.py>        # upload + compile + run in one call
Exit 125 = can't connect: run `python vm.py vm ip --save`, retry once, then stop and report.
Exit 124 = timeout: retry once with --timeout 300, then stop and report.
Never run `python vm.py vm revert/reset/stop/snapshot` unless the user asks.
Write only under /home/user/work/ on the VM. Never retry a failing command more than twice.
```

Also install the `vm-recovery` skill (step 3 above) — it is written as numbered steps with
expected outputs and stop conditions, which small models follow much better than prose.

## Guardrails to state to any agent

- Confine guest-side writes to one agreed directory (easy cleanup, can't clobber the VM).
- `vm revert / reset / stop / snapshot` change or destroy VM state — only on explicit request.
- Exit codes to branch on: remote rc passes through for `run`/`build-run`; `124` timeout;
  `125` can't connect / config error; `0` success.
