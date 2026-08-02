# Install

## Quick start

Install into the environment (ideally a venv) where your agent runs:
```
pip install .                                # from this repo; installs `vm` and `vm-init`
cd /path/to/your/project                     # where the agent works; config lives here
vm-init                                      # interactive: pick VMs, writes config + folders
vm vm doctor                                 # checks: config, vmrun, vmx, SSH, sudo
```

`vm-init` discovers the VMs VMware knows about, auto-detects OS + IP, prompts for the rest, and
creates `vmconfig.json` plus `staging/` and `provision/` in the current directory (`--agents` also
appends a "Test VM" section to `./AGENTS.md`). `vm` looks for that config in the working directory
— or `$VM_CONFIG`, or `--config PATH` — so run it from your project dir. It holds passwords and is
gitignored; never commit it. When every doctor check says `[PASS]` you're done: `vm docs` prints
the usage reference, from any directory, with no repo checked out.

## Wiring it into Claude Code

Three optional steps, each independent:

**1. Tell the agent the tool exists** — quickest is `vm-init --agents`, which drops a "Test VM"
section into your project's `AGENTS.md`. Or add it to `CLAUDE.md` by hand:
```md
## Test VM
Drive the test VM with the `vm` CLI (on PATH), run from this project dir.
Run `vm docs` once to read the verbs. Rules: confine guest writes to an agreed dir;
never run `vm vm revert/reset/stop/snapshot` unless explicitly asked or your team wrecked the state.
```

**2. Skip permission prompts** — in `.claude/settings.json` (project) or `~/.claude/settings.json`:
```json
{
  "permissions": {
    "allow": ["Bash(vm:*)", "PowerShell(vm:*)"]
  }
}
```
To keep destructive host verbs behind a prompt, allowlist only safe ones instead
(`Bash(vm run:*)`, `Bash(vm push:*)`, `pull:*`, `build-run:*`, `Bash(vm vm doctor)`, `vm vm ip:*`).

**3. Install the recovery skill** — makes `/vm-recovery` available (snapshot → reset → sync loop).
It ships inside the package, so no repo checkout is needed:
```
vm docs --install-skill              # -> ./.claude/skills/vm-recovery/SKILL.md
vm docs --install-skill ~            # global install instead
```

For other agents (Cursor, Aider, custom) it's the same three ideas: tell the agent to run
`vm docs`, allow `vm ...` in the harness's command allowlist, state the guardrails below.

## Small models (Haiku-class)

A small model may not reliably run `vm docs` and digest it. Instead of the pointer, paste this
self-contained snippet into its system prompt / CLAUDE.md — it needs no other reading:

```md
## Test VM — exact commands (the `vm` CLI is on PATH; run from the project dir)
vm run "<cmd>"                      # run on VM; exit code = the command's own rc
vm push <file>... /remote/dir/      # upload file(s)
vm pull /remote/file [local]        # download
vm build-run <src.c|.sh|.py>        # upload + compile + run in one call
Exit 125 = can't connect: run `vm vm ip --save`, retry once, then stop and report.
Exit 124 = timeout: retry once with --timeout 300, then stop and report.
Never run `vm vm revert/reset/stop/snapshot` unless the user asks.
Write only under /home/user/work/ on the VM. Never retry a failing command more than twice.
```

Also install the `vm-recovery` skill (step 3) — numbered steps with expected outputs and stop
conditions, which small models follow much better than prose.

## Guardrails to state to any agent

- Confine guest-side writes to one agreed directory (easy cleanup, can't clobber the VM).
- `vm revert / reset / stop / snapshot` change or destroy VM state — only on explicit request.
- Exit codes to branch on: remote rc passes through for `run`/`build-run`; `124` timeout;
  `125` can't connect / config error; `0` success.
