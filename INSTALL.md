# Installing vm.py into a coding-agent workflow

How to wire this tool into Claude Code (or a similar coding agent) so the agent can drive a test VM
with minimal friction. Two parts: **one-time machine setup**, then **agent integration**.

## 1. One-time setup (any environment)

1. Put this repo somewhere stable, e.g. `E:\Projects\vm_automation`.
2. Install the runtime dep: `pip install paramiko` (add `pytest` too if you want to run `tests/`).
3. Create your config from the template and fill it in:
   ```
   copy vmconfig.example.json vmconfig.json      # Windows;  cp on *nix
   ```
   Set each VM's `host`, `vmx`, `default_user`, and per-user `password`. `vmconfig.json` is
   **gitignored** because it holds passwords — never commit it.
4. Validate everything before handing it to an agent:
   ```
   python vm.py vm doctor
   ```
   All five checks should say `[PASS]` (config, vmrun.exe, vmx path, SSH, sudo).

## 2. Claude Code integration

Three things make the tool first-class for the agent: a **pointer** so it knows the tool exists, a
**permission allowlist** so calls don't prompt every time, and the **recovery skill**.

### a. Tell the agent about the tool (CLAUDE.md)
Add a short pointer to your project's `CLAUDE.md` (or run it from inside this repo, which already
has `README.md`). Keep it terse — the agent reads `README.md` for the full verb list:
```md
## Test VM
Drive the test VM with `python vm.py <verb>` (run from E:\Projects\vm_automation).
Read README.md once for the verbs. Rules: confine guest writes to an agreed dir;
never run `vm revert/reset/stop/snapshot` unless explicitly asked.
```

### b. Allowlist the command (fewer permission prompts)
In `.claude/settings.json` (project) or `~/.claude/settings.json` (global), add:
```json
{
  "permissions": {
    "allow": [
      "Bash(python vm.py:*)",
      "PowerShell(python vm.py:*)"
    ]
  }
}
```
This lets the agent call any `python vm.py ...` verb without a prompt. If you want to withhold the
destructive host verbs, allowlist only the safe ones instead (e.g. `Bash(python vm.py run:*)`,
`Bash(python vm.py push:*)`, `Bash(python vm.py build-run:*)`, `Bash(python vm.py vm doctor)`,
`Bash(python vm.py vm ip:*)`) and leave `vm reset/revert/stop/snapshot` to prompt each time.

### c. Install the recovery skill
`SKILL.md` is a ready-to-use Claude Code skill (it has the required frontmatter). Install it as
`vm-recovery`:
```
# project-scoped:
mkdir .claude\skills\vm-recovery  &  copy SKILL.md .claude\skills\vm-recovery\SKILL.md
# or global:  ~/.claude/skills/vm-recovery/SKILL.md
```
The agent can then invoke `/vm-recovery` to run the snapshot → reset → sync loop.

## 3. Other coding agents (Cursor, Aider, custom harnesses)

The tool is just a CLI, so integration is the same three ideas without Claude Code's specific files:
- **Expose usage:** give the agent `README.md` as the tool's reference (paste it into the system
  prompt or point the agent at the file).
- **Allow the command:** permit `python vm.py ...` in whatever command-allowlist the harness uses.
- **State the guardrails** in the system prompt: confine guest-side writes to an agreed directory,
  and don't call `vm revert/reset/stop/snapshot` unless the user asks.

## 4. Verify the integration

Have the agent run these read-only calls; if they succeed the wiring is good:
```
python vm.py vm doctor
python vm.py run "uname -a"
python vm.py vm ip
```

## Guardrails worth stating to any agent
- **Confinement:** keep guest-side files under one agreed directory (e.g. a project temp dir) so
  runs are easy to clean up and can't clobber the rest of the VM.
- **Destructive host verbs:** `vm revert`, `vm reset`, `vm stop`, `vm snapshot` change or destroy VM
  state — only on explicit request.
- **Exit codes** the agent can branch on: remote rc passes through for `run`/`build-run`; `124` =
  timeout; `125` = can't connect / config error; `0` = success.
