# Kyverno

Policy engine for Kubernetes with production Secret synchronization and resource auditing.

## Overview

This chart wraps upstream Kyverno. Its defaults render a resource-audit policy
and a production Secret-clone policy. OpenTelemetry injection is disabled.

## Architecture

The chart deploys four Kyverno controllers:

- **Admission Controller** - Intercepts API server requests to mutate and validate resources against policies
- **Background Controller** - Applies policies retroactively to existing resources (not just new ones)
- **Cleanup Controller** - Manages lifecycle of policy reports
- **Reports Controller** - Generates policy compliance reports

Custom policies included:

- **OTel Injection** (`inject-otel-env-vars`) - Disabled by default; when enabled, injects OTLP endpoint and protocol into workloads
- **Require Resource Requests** (`require-resource-requests`) - Audits workloads for missing CPU/memory resource requests
- **Clone Monolith Workflow Secrets** (`clone-production-monolith-workflow-secrets`) - Clones the monolith Postgres app secret and the other job-pod Secrets into `monolith-workflows`, triggered on each source Secret so a recreated source is re-cloned

The Secret policy uses `synchronize: true` and `generateExisting: true` on
source-Secret matches so creation and rotation both update the clones. It is
generate-only and has no Audit validation mode. #7034 retires the monolith-dev
rule and renames the policy because synchronized rule membership is immutable.
Every production rule and the controller aggregation grants are preserved.
This is repository configuration; no live migration or Secret deletion is
performed by the retirement PR.

## Configuration

| Value                                       | Description                                | Default                                               |
| ------------------------------------------- | ------------------------------------------ | ----------------------------------------------------- |
| `otelInjection.enabled`                     | Enable OTel env var injection policy       | `false`                                               |
| `otelInjection.endpoint`                    | OTel collector endpoint                    | `""`                                                  |
| `otelInjection.protocol`                    | OTel exporter protocol                     | `grpc`                                                |
| `otelInjection.targetKinds`                 | Resource kinds to inject into              | `[Deployment, StatefulSet, DaemonSet]`                |
| `kyverno.admissionController.replicas`      | Admission controller replicas              | `1`                                                   |
| `kyverno.features.policyExceptions.enabled` | Enable PolicyException CRD                 | `true`                                                |
