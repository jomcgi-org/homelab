# GKE store validation operator runbook

This runbook stages the repository side of #6193. Nothing in this directory
creates a bucket, budget, credential, notification channel, or IAM binding.
`values-store-validation-gke.yaml` is not referenced by either Argo CD
Application or either kustomization. The production Application remains on
`h0melab-ember-bases`, and this stage defines no production lifecycle deletion
rule. The live production bucket still requires verification before and after
the validation run.

The overlay explicitly disarms base retention, remote base retention, warmth
retention, and direct S3 warmth GC inherited from the base dev values. Confirm
those environment gates are absent in the rendered control plane. The
seven-day GCS bucket lifecycle below is the only deletion mechanism intended
for this validation release.

The validation bucket is `h0melab-ember-bases-dev`. Its only lifecycle action
deletes objects at age seven days. The budget is an alerts-only USD 15 monthly
budget for the `h0melab` project's entire Cloud Storage service, identified by
`services/95FF-2EF5-5EA1`. It is not limited to either EmberVM bucket, and a
budget alert does not cap or stop spending.

## Resolve external inputs

Use an account with read access first. Record command output in the #6193
operator evidence before making any change.

```bash
validation_project_id=h0melab
validation_bucket=h0melab-ember-bases-dev
production_bucket=h0melab-ember-bases

gcloud projects describe "$validation_project_id" \
  --format='value(projectNumber)'
gcloud billing projects describe "$validation_project_id" --format=json
gcloud alpha monitoring channels list --project="$validation_project_id" \
  --filter='type=email' --format=json
```

Resolve and record these provider resource names. Do not commit their secret
material:

- `billingAccounts/<billing-account-id>` from the project's billing binding.
- `projects/<project-number>` from the project description. Budget filters use
  the numeric project resource, not `projects/h0melab`.
- `projects/<project-number>/notificationChannels/<channel-id>` for an enabled
  email notification channel whose recipient the operator verified.
- The existing 1Password item path that will deliver GCS HMAC keys to the
  `embervm-store-validation-gcs` Kubernetes Secret. The checked-in overlay
  deliberately leaves `itemPath` empty and creates no credential object.

`cloud-storage-budget.json` records these as `OPERATOR_*` inputs and pins the
Cloud Billing Budget API, the Cloud Storage service resource, the current-spend
threshold, and the amount. Replace the placeholders only in an operator-owned
temporary file or a later reviewed activation change.

## Inspect production before creating the dev bucket

Read the production bucket and save the exact location and default storage
class. Also verify that production has no lifecycle delete policy. Stop if the
location or class is empty, or if a production deletion rule exists.

```bash
gcloud storage buckets describe "gs://$production_bucket" --format=json \
  > /tmp/embervm-production-bucket-before.json
jq '{location, default_storage_class, lifecycle_config}' \
  /tmp/embervm-production-bucket-before.json
```

Before creation, prove the validation name is unused:

```bash
if gcloud storage buckets describe "gs://$validation_bucket" --format=json; then
  echo "validation bucket already exists, inspect ownership before continuing"
else
  echo "validation bucket name is available"
fi
```

## Create and harden only the dev bucket

After review of the captured production description, copy its exact location
and storage class into task-specific shell variables. The create command must
name only the dev bucket.

```bash
validation_location=OPERATOR_VERIFIED_PRODUCTION_LOCATION
validation_storage_class=OPERATOR_VERIFIED_PRODUCTION_STORAGE_CLASS

gcloud storage buckets create "gs://$validation_bucket" \
  --project="$validation_project_id" \
  --location="$validation_location" \
  --default-storage-class="$validation_storage_class" \
  --uniform-bucket-level-access \
  --public-access-prevention=enforced
```

Read back the bucket and IAM policy before applying a lifecycle. Confirm the
location and storage class exactly match production, uniform bucket-level
access is enabled, public access prevention is enforced, and neither
`allUsers` nor `allAuthenticatedUsers` appears in IAM.

```bash
gcloud storage buckets describe "gs://$validation_bucket" --format=json \
  > /tmp/embervm-validation-bucket-before-lifecycle.json
gcloud storage buckets get-iam-policy "gs://$validation_bucket" --format=json \
  > /tmp/embervm-validation-bucket-iam.json
jq '{location, default_storage_class, uniform_bucket_level_access,
     public_access_prevention, lifecycle_config}' \
  /tmp/embervm-validation-bucket-before-lifecycle.json
if grep -Eq 'allUsers|allAuthenticatedUsers' \
  /tmp/embervm-validation-bucket-iam.json; then
  echo "public IAM member found, stop"
  exit 1
fi
```

Apply the checked-in lifecycle only to the dev bucket, then read back both
buckets. The dev output must contain exactly one `Delete` action at age 7. The
production output must still contain no lifecycle deletion rule.

```bash
gcloud storage buckets update "gs://$validation_bucket" \
  --lifecycle-file=projects/embervm/deploy/store-validation/h0melab-ember-bases-dev-lifecycle.json
gcloud storage buckets describe "gs://$validation_bucket" --format=json \
  > /tmp/embervm-validation-bucket-after-lifecycle.json
gcloud storage buckets describe "gs://$production_bucket" --format=json \
  > /tmp/embervm-production-bucket-after.json
jq '.lifecycle_config' /tmp/embervm-validation-bucket-after-lifecycle.json
jq '.lifecycle_config' /tmp/embervm-production-bucket-after.json
```

## Create the alerts-only budget

First list existing budgets on the verified billing account and avoid creating
a duplicate display name. Confirm the notification channel is enabled and the
recipient is correct. Substitute the three operator inputs from
`cloud-storage-budget.json` into the command below.

```bash
validation_billing_account=OPERATOR_VERIFIED_BILLING_ACCOUNT_ID
validation_project_number=OPERATOR_VERIFIED_PROJECT_NUMBER
validation_notification_channel_id=OPERATOR_VERIFIED_CHANNEL_ID
validation_notification_channel=projects/$validation_project_number/notificationChannels/$validation_notification_channel_id

gcloud billing budgets list \
  --billing-account="$validation_billing_account" --format=json
gcloud alpha monitoring channels describe "$validation_notification_channel_id" \
  --project="$validation_project_id" --format=json
```

Only after those inspections, create the Cloud Storage service budget:

```bash
gcloud billing budgets create \
  --billing-account="$validation_billing_account" \
  --display-name='h0melab Cloud Storage monthly alert' \
  --budget-amount=15USD \
  --calendar-period=month \
  --filter-projects="projects/$validation_project_number" \
  --filter-services=services/95FF-2EF5-5EA1 \
  --threshold-rule=percent=1.0,basis=current-spend \
  --notifications-rule-monitoring-notification-channels="$validation_notification_channel" \
  --disable-default-iam-recipients \
  --format=json > /tmp/embervm-store-validation-budget-created.json

validation_budget_name=$(jq -er '.name' \
  /tmp/embervm-store-validation-budget-created.json)
gcloud billing budgets describe "$validation_budget_name" --format=json \
  > /tmp/embervm-store-validation-budget-readback.json
jq '{displayName, amount, budgetFilter, thresholdRules, notificationsRule}' \
  /tmp/embervm-store-validation-budget-readback.json
```

Read the created budget back. Verify USD 15, monthly calendar period, the
numeric `h0melab` project resource, Cloud Storage service
`services/95FF-2EF5-5EA1`, one 100 percent `CURRENT_SPEND` threshold, and the
enabled notification channel. This is alerting only. It does not cap spending
and has no Pub/Sub automation that could disable services.

## Render and activate an isolated validation release

Before activation, render the chart with the dev values, then the existing GKE
overrides, then the inactive validation overlay. This order keeps the dev
workload scope, applies the hub's no-Cilium, unpinned-brick, and scratch-prep
requirements, and finally replaces the production GKE store settings. Inspect
every control-plane, noded, and rootfs-builder store setting.
Every bucket must be `h0melab-ember-bases-dev`, no rendered manifest may contain
`h0melab-ember-bases` as a distinct value, and every store credential reference
must be a required reference to `embervm-store-validation-gcs`. The render must
not contain `EMBERVM_BASE_RETENTION_SWEEP`,
`EMBERVM_BASE_RETENTION_DISK_DRIVEN`,
`EMBERVM_BASE_REMOTE_RETENTION_SWEEP`, `EMBERVM_WARMTH_RETENTION_SWEEP`, or
`EMBERVM_WARMTH_S3_GC`.

```bash
helm template embervm-store-validation projects/embervm/chart \
  --namespace embervm-store-validation \
  --values projects/embervm/chart/values.yaml \
  --values projects/embervm/dev/deploy/values.yaml \
  --values projects/embervm/deploy/values-gke.yaml \
  --values projects/embervm/dev/deploy/values-store-validation-gke.yaml \
  > /tmp/embervm-store-validation.yaml
grep -nE 'EMBERVM(_NODED)?_STORE_(ENDPOINT|BUCKET)|secretKeyRef|name: embervm-store-validation-gcs' \
  /tmp/embervm-store-validation.yaml
```

Do not activate while the overlay's 1Password item path is empty. Provision or
verify the dev HMAC credential and its 1Password delivery out of band, without
committing keys. A later explicit, reviewed activation must put the verified
item path in the overlay or otherwise create the exact named Secret. Required
SecretKeyRefs deliberately prevent the release containers from starting when
the Secret or either key is missing.

Activate only as a separate release in an isolated namespace. Do not add this
overlay to either checked-in Application or kustomization. After activation:

1. Read every rendered and live control-plane, noded, and rootfs-builder bucket.
2. Write and read a disposable validation object through EmberVM.
3. Confirm the object exists only in `h0melab-ember-bases-dev`.
4. Re-read the production bucket object inventory and lifecycle, and confirm
   neither changed.
5. Attach the redacted render, bucket descriptions, IAM policy check, lifecycle
   readbacks, budget readback, Secret delivery status, and write/read evidence
   to #6193.

Keep the issue open until every live check above has evidence.
