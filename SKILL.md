---
name: vm-recovery
description: Recover a test VM to its clean snapshot when it is in a bad state, or when SSH/run fails (e.g. after a revert changed the guest IP). Uses the vm.py CLI. Follow the numbered steps exactly.
---

# Skill: VM recovery

Use when: a risky test broke the VM, or `run` fails with exit code `125` (can't connect).

Run every command from the vm.py directory (e.g. `E:\Projects\vm_automation`).

## Steps

1. Try the cheap fix first — the guest IP may just have changed:
   ```
   python vm.py vm ip --save
   python vm.py run "true"
   ```
   If `run` exits `0`, the VM is fine. **Stop here.**

2. Full reset (revert to clean snapshot + power on + wait for SSH + refresh IP):
   ```
   python vm.py vm reset
   ```
   Expected: a success line and exit `0`. If it fails, run `python vm.py vm doctor`,
   report which check says `[FAIL]`, and stop — do not retry in a loop.

3. Re-upload your files — reset wiped guest-side state back to the snapshot:
   ```
   python vm.py sync ./staging
   ```
   (or `push` the specific files you need).

4. Confirm: `python vm.py run "true"` exits `0`. Recovery is done.

## One-time prerequisite (only if `vm reset` says the snapshot is missing)

With the VM in a known-good state:
```
python vm.py vm snapshot clean
```
(the name must match `snapshot` in `vmconfig.json`), then go back to step 2.
