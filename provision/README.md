# provision/ — tools staged into the guest on first run

Drop files here and they get copied into the guest's tools dir the first time vm.py
connects (and are made executable on Linux). No manifest — the folder *is* the config.

## Layout

```
provision/
  <vm-name>/     # e.g. ubuntu24/  — matches the VM name in vmconfig.json (preferred)
  <os>/          # e.g. linux/ or windows/  — fallback when there's no per-VM folder
```

For a given VM, vm.py uses `provision/<vm-name>/` if it exists, else `provision/<os>/`.
Everything in that folder is synced (recursively) to the guest **tools dir**
(`tools_remote` in config, default `<home>/tools`), which `run` automatically prepends
to `PATH`. So `provision/ubuntu24/strace` → `run "strace -V"` just works.

## Optional setup script

If the folder has a `setup.sh` (Linux) or `setup.ps1` (Windows) at its root, it runs once
after the files are copied — for anything a plain copy can't do (installers, registry, apt).

- `setup.sh` runs over SSH as the default user (use `sudo` inside if needed).
- `setup.ps1` runs **elevated** over the VMware Tools channel — the right place for an
  MSVC/Build Tools installer, which SSH can't elevate.

Keep these scripts simple and idempotent. If they start growing per-host conditionals,
that's the sign to move to a real provisioner (Ansible, etc.) instead.

## When it runs

- Automatically on the first guest command, if the folder changed since last time
  (tracked by a hash in `<tools>/.provisioned`). Unchanged = fast no-op.
- On demand: `python vm.py --vm NAME vm provision` (add `--force` to re-stage).

## Bake it into the snapshot

The intended lifecycle: bootstrap → `vm provision` → `vm snapshot clean`. After that every
`vm reset` restores a fully-provisioned guest instantly instead of re-staging.
