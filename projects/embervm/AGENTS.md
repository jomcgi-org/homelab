# AGENTS.md - embervm

Scoped guidance for `projects/embervm`, for any agent. The repo-root `AGENTS.md`
still applies. `ARCHITECTURE.md` in this directory has the design.

- Base snapshots clone guest process memory, so a restored guest is
  bit-identical to the base. Restore-time triggers must derive from external
  state the restore changed (device superblock, mount table), never from
  in-process state.
- The control plane's log formatter renders only the whitelisted `@meta_keys`
  in `control/lib/embervm/log_formatter.ex`; a new structured log field must be
  added there or it is dropped.
