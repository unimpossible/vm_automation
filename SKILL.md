# Skill: VM recovery (snapshot/reset loop)

**Symptom:** VM is in a bad state (broken by a risky test), or SSH/`run` fails after a
`vm revert` because the guest got a new IP.

**One-time setup:** once the VM is known-good, capture a clean snapshot:
```
python vm.py vm snapshot clean
```
(or whatever name matches `snapshot` in `vmconfig.json`).

**Recovery (after anything risky):**
```
python vm.py vm reset
```
This reverts to the clean snapshot, powers on, waits for SSH, and refreshes the IP into
`vmconfig.json` (atomic write) — no manual IP editing.

**Then re-sync your files** (reset wipes guest-side state back to the snapshot):
```
python vm.py sync ./staging
```
(or `mount` if using the WSL live-bind).

That's the whole loop: `vm snapshot clean` once → `vm reset` → `sync` after every risky operation.
