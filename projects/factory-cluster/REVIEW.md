# Review: the factory cluster

Includes: projects/platform-factory/REVIEW.md

Rules for `projects/factory-cluster/` on top of the root `REVIEW.md`.

- **Node changes.** A change under `nodes/` takes effect on a rerun of
  `bootstrap-nodes.sh`, and the PR says whether k3s restarts: k3s reads its
  config only at start (#7894). Servers restart one at a time; two down
  loses etcd quorum.
- **Destructive scripts.** `reset-node.sh` keeps its `--yes <hostname>`
  confirmation, and a new command that deletes cluster state names exactly
  what it removes.
