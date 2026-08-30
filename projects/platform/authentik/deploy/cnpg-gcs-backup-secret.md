# Authentik CNPG GCS backup credential

This note records the existing out-of-band credential used by CloudNativePG:

- 1Password item path: `vaults/k8s-homelab/items/cnpg-gcs-backups-authentik`
- Field name: `service-account-key.json`
- Service account: `cnpg-backup-authentik@h0melab.iam.gserviceaccount.com`
- Bucket: `gs://h0melab-cnpg-backups` in `europe-west2`

The 1Password item and service account already exist. No setup is needed. This
file documents the credential that the chart syncs into the cluster.
