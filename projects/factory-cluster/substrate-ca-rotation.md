# Rehearse a Substrate CA rotation

Factory roots expire on 2027-11-13. HOME-714 covers the local exercise. HOME-81 owns any separately reviewed factory change. Keep its existing permissions and approval requirements.

## Inventory and ownership

The factory cluster operator owns the generated Secrets. The component owners below must confirm signer and verifier convergence for their pool.

| Namespace | Secret | Signing component | Verification boundary |
| --- | --- | --- | --- |
| podcertificate-controller-system | service-dns-ca-pool | podcertificate controller | Clients checking service DNS certificates |
| podcertificate-controller-system | pod-identity-ca-pool | podcertificate controller | Peers checking pod identities |
| podcertificate-controller-system | postgres-ca-pool | podcertificate controller | Database TLS peers using the Postgres signer |
| ate-system | actor-id-ca-pool | ate-api-server | Egress gateway reads the separate actor-id-ca-certs bundle |
| ate-system | egress-mitm-ca-pool | Egress gateway reads tls.crt and tls.key | ate-controller publishes the egress-mitm.ate.dev trust bundle to actors |

The first three pools use Ed25519 and the last two use P-256. The actor JWT pool has a separate lifecycle. See [bootstrap-substrate.sh](bootstrap-substrate.sh).

## Upstream support checked 2026-11-21

Both the [deployed v1.0.1 admin commands](https://github.com/kagent-dev/substrate/blob/7ce6f439239c568285f6d897aaa4afd47752c775/cmd/kubectl-ate/internal/cmd/admin.go) and the [fork head inspected](https://github.com/kagent-dev/substrate/blob/fd66419e10fddc6209ee2c4c4135fafbb17fe845/cmd/kubectl-ate/internal/cmd/admin.go) create a CA pool and expose add/activate/remove for JWT keys only. Recheck before a live change. The [deployed pool implementation](https://github.com/kagent-dev/substrate/blob/7ce6f439239c568285f6d897aaa4afd47752c775/internal/localca/localca.go) selects ActiveForSigning by ID and defaults to the first CA when empty.

## Procedure for one pool
<!-- invariant: CLUS-6 -->

1. **Baseline.** Record the pool ID, public root fingerprints, validity, active signer and latest possible leaf expiry. Check an existing leaf against the mounted bundle. Preserve a restricted, restorable copy of the pool and associated projections. Stop here if ownership, backups or the active ID cannot be verified.
2. **Expand trust.** Append a fresh CA of the same key type with a distinct ID to CAs and explicitly select the old ID. Publish every RootCertificateDER as a PEM root. For actor identity, refresh actor-id-ca-certs as in bootstrap. For interception, leave the gateway TLS pair on the old CA. Check both fingerprints at every consuming replica and an old-leaf handshake before proceeding. If propagation fails, keep signing with the old CA and restore the baseline while no new leaves exist.
3. **Switch signing.** Preserve both CAs and change ActiveForSigning to the new ID. For interception also replace the gateway TLS pair together with the matching new certificate and key. Verify signer reload on every replica by checking the issuer of fresh leaves. Verify old and new leaves against the actual consumer bundles. If either fails, switch back to the old signer and matching TLS pair, retain both roots and repeat the propagation checks.
4. **Drain.** Record when the last replica stopped old issuance. Wait beyond the latest old-leaf NotAfter plus clock skew, or use last possible old issuance plus a verified maximum lifetime and skew. A rollback that resumes old issuance resets the deadline. If expiry cannot be bounded, retain both roots.
5. **Retire.** Remove the old CA from the pool and republish trust. Confirm only the new fingerprint at each consumer, successful new-leaf verification and rejection of the old chain. If consumers fail, restore the dual-root pool and bundles with the new signer selected. Confirm convergence before retrying.

## Local evidence

Run `bash projects/factory-cluster/rehearse-substrate-ca.sh` with Bash, jq and OpenSSL 3. It uses a private temporary directory and models the actor pool JSON and its public PEM projection. It signs leaves from the selected entries, checks both during overlap, advances the verification clock two days past issuance, and checks the five-day leaf after retirement. A final check at the original time isolates old-root removal from one-day leaf expiry. Expected negative checks report certificate expiry and then an unavailable local issuer. Exit zero means all assertions passed. Cluster reconciliation and process reload require the per-replica checks above.
