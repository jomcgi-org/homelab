# Grimoire GCP Bootstrap

> **Status: out of scope, not deployed.** This directory is the March-era standalone
> Firestore/Cloud Run app. It predates the current design and is slated for
> decommission. Nothing here is deployed. The live ingest path is S3 NDJSON
> manifests processed by Argo CronWorkflows inside the monolith; see
> [../README.md](../README.md). The rest of this file is kept as historical
> reference only.

One-time setup for GCP serverless infrastructure (Firestore, Cloud Storage, Cloud Run).

## Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- A GCP billing account linked (required for Tier 1 Gemini Live concurrent sessions)

## Setup

```bash
make setup
```

This creates the GCP project, enables APIs, provisions Firestore, Cloud Storage, and Artifact Registry.

After setup:

1. Create a Gemini API key at https://aistudio.google.com/apikey
2. Store it in 1Password (vault: **Homelab**, item: **grimoire-gemini-api-key**, field: **key**)
3. The 1Password Operator syncs it to the cluster automatically via a `OnePasswordItem` resource

## Deploy

```bash
make deploy-api          # Build + deploy Cloud Run API
make upload-pdf FILE=./phb.pdf  # Upload a sourcebook PDF
make ingest PDF=gs://grimoire-sourcebooks/phb.pdf  # Process it
```

## Teardown

```bash
make teardown            # Deletes all GCP resources (keeps the project)
```
