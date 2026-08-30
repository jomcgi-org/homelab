# GKE Cutover Window Runbook

The downtime window that moves the app workloads from the home cluster to the
GKE hub (issue #5458, part of #4964). Everything here was staged and rehearsed
on 2026-08-30: the GKE destinations are deployed dormant (#5464), CNPG backups
archive to GCS and the recovery path is proven (#5465), and the R2 object flip
is review-clean and waiting (#5462). The window turns those staged pieces on
and wipes the home cluster only after verification.

Preconditions, all met before opening the window:

- `gs://h0melab-cnpg-backups/monolith-pg/monolith-pg/base/` holds a completed
  base backup (the 02:00 ScheduledBackup writes the first one; WAL alone is
  not restorable).
- The R2 copy is complete including `grimoire` and `stargazer-archive` (see
  step 1).
- PR #5462 is merged and rolled out at home (proves the R2 flip against live
  traffic while rollback is still trivial).

## 1. Finish the object copy (before the window)

From node-4 (fastest uplink), with 1Password unlocked for `op`:

```bash
{ op read 'op://k8s-homelab/r2-s3-credentials/access-key-id'; \
  op read 'op://k8s-homelab/r2-s3-credentials/secret-access-key'; } | \
  ssh node-4 './r2copy.sh copy'
```

`~/r2copy.sh` on node-4 reads the key pair from stdin, never argv. It copies
every bucket; already-copied objects are cheap re-checks. Most buckets were
prefilled through wrangler, and those objects carry NEW modtimes, so any
rclone comparison here and in step 5 MUST use `--size-only` or it re-copies
~31 GiB for nothing.

Verify by object count per bucket (`wrangler r2 bucket info <bucket>`, counts
lag a few minutes) against the SeaweedFS side
(`aws --endpoint-url http://10.43.44.228:8333 s3 ls s3://<bucket> --recursive
--summarize` with the dummy `duckdb` creds).

## 2. Merge and roll the R2 flip (before the window)

Enqueue #5462 (`gh pr merge 5462 --auto --rebase`). Done means live per the
pr-workflow skill: merged, the chart write-back landed, monolith rolled
through Kargo, monolith-public rolled on its write-back, pods answer. Spot
checks that prove the flip end to end:

- an image on jomcgi.dev trips page renders (imgproxy reads R2 through the
  Cloudflare CIDR egress allow),
- a public artifact page loads (public tier reads R2 with the read-only
  token),
- `stars-refresh` CronWorkflow next run completes (job pods read the
  Kyverno-cloned `monolith-r2-s3` Secret in `monolith-workflows`).

## 3. Stop writers, final WAL switch

Announce the window. Then, in order:

1. Scale home writers to zero BY VALUES COMMIT (never kubectl): backend
   replicas, CronWorkflow suspends. The point is no new rows and no new blob
   writes anywhere.
2. Final object delta from node-4, `--size-only` (minutes; it is only the
   writes since step 1).
3. Force the last WAL out of each home cluster. Postgres switches WAL on the
   checkpoint that a CNPG backup takes, so the cleanest lever is one final
   on-demand backup per cluster; otherwise
   `psql -c "SELECT pg_switch_wal();"` on each primary and wait for
   `ContinuousArchiving: True` to reconfirm.

## 4. Recover and promote on GKE

authentik-pg and context-forge-pg on the hub were already bootstrapped by
recovery during the rehearsal, but from the REHEARSAL point in time. All three
clusters get a fresh recovery so the window's final WAL is included:

1. Advance the gke-apps chart pins (`projects/gke-apps/*/application.yaml`
   `targetRevision`) to the current published versions. The pins are bootstrap
   floors that the write-back does not maintain; this is the one manual bump.
   Re-enable the GKE monolith backup in the same commit
   (`projects/monolith/deploy/values-gke.yaml`, drop `enabled: false`; the
   distinct `serverName: monolith-pg-gke` is already there).
2. On the hub, delete each CNPG Cluster and its PVCs (`kubectl delete
   cluster.postgresql.cnpg.io <name>` plus the `<name>-N` PVCs, hub context
   only, never home). ArgoCD recreates them; with the advanced pin the
   monolith Cluster now carries `bootstrap.recovery` and re-bootstraps from
   GCS including the final WAL. This is deliberate: a Cluster only consults
   `bootstrap` at creation, so recovery config changes need the delete.
3. Wait for `Cluster in healthy state`, then verify data the same way the
   rehearsal did: row counts against known tables, and the `-app` Secrets
   exist (CNPG 1.30 overwrites the restored role's password from the minted
   Secret; no manual ALTER).
4. Scale the GKE workloads up by values commit: backend replicas, authentik
   server/worker, context-forge replicaCount, CronWorkflow suspends off,
   `teamMapping.enabled` back on. `whatsapp.enabled` stays false until the
   home cluster is off (single-session singleton).
5. Flip ingress (Cloudflare) to the hub and verify the public and private
   surfaces answer.

Known cosmetic state until step 4.2: the hub monolith Application shows the
Cluster object OutOfSync (CNPG self-mutates its spec under the old pin).

## 5. Verify, then wipe

Nothing at home is wiped until every line below holds:

- [ ] All hub Applications Synced and Healthy.
- [ ] Row counts on the three recovered clusters match the home clusters at
      writer-stop (capture both sides during step 3).
- [ ] Public tier: trips images, artifact pages, stars API answer from GKE.
- [ ] Private tier: authentik login works, agents console loads, one
      CronWorkflow has completed a scheduled run.
- [ ] GKE backups are archiving: `authentik-pg-gke`, `context-forge-pg-gke`,
      `monolith-pg-gke` prefixes growing in `gs://h0melab-cnpg-backups`.
- [ ] R2 object counts match the final delta.
- [ ] One full day of history is NOT required: GCS keeps the home archives
      under the original serverNames as the fallback.

Only then: wipe the home nodes. The home SeaweedFS dies with them; its charts
are deleted, not ported.

## Deferred and follow-ups

- Node-4 inference bridge (#5457): the GKE overlays point all three inference
  URLs at `inference-bridge.tailscale.svc.cluster.local:8080`, which resolves
  nowhere until #5457 lands the Service. Embeddings need their own port or
  Service there, and the embervm egress allowlist entry lands WITH the bridge
  (one brick roll).
- EmberVM on GKE (#5459): the `embervm` namespace is pre-created on the hub
  (holds only the monolith FaaS RBAC pair until then). The FaaS zip lane
  fetches R2 unsigned until noded's store moves (documented at
  `faas.s3ReadBase` in the monolith chart values).
- Rotate the `homelab-rw` R2 token (deliberately left in service on
  2026-08-30; the secret is only in session transcripts).
- Recovery IAM: the three `cnpg-backup-*` service accounts need
  `roles/storage.legacyBucketReader` on the bucket in addition to
  `objectAdmin` (barman's recovery pre-check calls `storage.buckets.get`).
  Already granted; keep it if the bucket is ever recreated.
