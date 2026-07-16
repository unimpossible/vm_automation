# NL usage eval — run 01

**Model:** Sonnet
**Date:** 2026-07-15
**Goal:** Test whether an agent, given only the README and a natural-language task,
can correctly drive `vm.py` end-to-end against the live `ubuntu24` VM. This measures
doc quality and tool ergonomics, not the tool's correctness (that's what tests/ covers).

**Guardrails given to the agent (same ones a real operator would state):**
- Only touch the VM under `/home/user/Desktop/vm_automation_test/`.
- Do not run destructive host verbs (revert/reset/stop/start/snapshot).

## Prompt given to the agent (verbatim)

> I have a Linux test VM already running. There's a command-line tool for driving it at
> `E:\Projects\vm_automation\vm.py` — read its `README.md` first to learn how it works,
> then use it (via `python vm.py ...`) to do the following, reporting what you find at each step:
>
> 1. Confirm the VM is healthy and reachable.
> 2. Tell me which user account and Linux distro/kernel it's running.
> 3. Write a short C program locally that prints the process uid and the system page size,
>    then compile and run it *on the VM* and show me the output.
> 4. Create a small text file, get it onto the VM, and prove it arrived intact (byte-for-byte).
> 5. Report the VM's current IP address.
>
> Keep anything you create on the VM under `/home/user/Desktop/vm_automation_test/`.
> Don't run any command that would revert, reset, or power off the VM.
> At the end, give me a numbered report of each step, the exact `vm.py` commands you ran,
> and whether each succeeded.
