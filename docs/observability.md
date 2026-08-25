# Observability Architecture

This document describes the automatic observability setup in the cluster.

## Overview

Every service gets automatic observability through three layers:

1. **OTEL Environment Variables** (Kyverno) - Endpoint configuration for all workloads
2. **OpenTelemetry Operator** - Language-specific auto-instrumentation (Go, Python, Node.js)
3. **Cilium Hubble** - Network-level flow and HTTP metrics from the eBPF datapath, no sidecars

## Pod Creation Flow

The following diagram shows how observability is automatically added to every pod:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Pod Creation Request                         │
│                    (kubectl apply / ArgoCD sync)                    │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Layer 1: Kyverno Policies                        │
├─────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────┐                                       │
│  │  OTEL Injection Policy   │                                       │
│  ├──────────────────────────┤                                       │
│  │ Adds env vars:           │                                       │
│  │ - OTEL_EXPORTER_         │                                       │
│  │   OTLP_ENDPOINT          │                                       │
│  │ - OTEL_EXPORTER_         │                                       │
│  │   OTLP_PROTOCOL=grpc     │                                       │
│  └──────────────────────────┘                                       │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│             Layer 2: OpenTelemetry Operator (opt-in)                │
├─────────────────────────────────────────────────────────────────────┤
│  Per-namespace Instrumentation custom resources (CRs) inject:       │
│  - Go: eBPF auto-instrumentation (autoinstrumentation-go)           │
│  - Python: auto-instrument init container                           │
│  - Node.js: require-hook init container                             │
│                                                                     │
│  Auto-instrumentation is enabled cluster-wide via the OpenTelemetry │
│  operator and Kyverno OTEL injection; see                           │
│  projects/platform/opentelemetry-operator                           │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Running Pod                                 │
├─────────────────────────────────────────────────────────────────────┤
│  ┌────────────────────┐                                             │
│  │  Application       │   Pod network traffic flows through the     │
│  │  Container         │   Cilium eBPF datapath: Hubble records      │
│  ├────────────────────┤   flows and HTTP metrics with no sidecar    │
│  │ OTEL env vars set  │   in the pod.                               │
│  │ OTel SDK injected  │                                             │
│  │ (if namespace opted│                                             │
│  │  into Operator)    │                                             │
│  └────────────────────┘                                             │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
                       ┌──────────────────┐
                       │  SigNoz Platform │
                       ├──────────────────┤
                       │ - Traces         │
                       │ - Metrics        │
                       │ - Logs           │
                       └──────────────────┘
```

## Automatic Observability (Kyverno Policies)

### 1. OTEL Environment Variables (Application-Level)

- **All workloads** receive OTEL env vars automatically
- `OTEL_EXPORTER_OTLP_ENDPOINT` → SigNoz collector
- `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`
- Applications with OTEL SDKs get automatic instrumentation
- Applications without OTEL SDKs ignore the vars (harmless)
- **Policy:** `projects/platform/kyverno/templates/otel-injection-policy.yaml`

### 2. OTel Operator Auto-Instrumentation (Language-Level)

- **Opt-in per namespace** via `Instrumentation` CRDs
- The OpenTelemetry Operator watches for these CRDs and injects language-specific init containers
- **Go:** eBPF-based, no code changes needed, instruments at the kernel level
- **Python:** Injects `autoinstrumentation-python` init container that patches the runtime
- **Node.js:** Injects `autoinstrumentation-nodejs` init container with require hooks
- Kyverno sets the OTEL endpoint; the Operator provides the SDK. They complement each other
- **Configuration:** `projects/platform/opentelemetry-operator/` with namespace list in values

### 3. Cilium Hubble (Network-Level)

- The Cilium eBPF datapath sees all pod traffic; no proxy sidecars, no injection
- Hubble exports flow and HTTP metrics (e.g. `hubble_httpv2_requests_total`),
  which SigNoz scrapes and the error-rate alerts read
- Drop metrics carry destination context (`destination_namespace`,
  `traffic_direction`, requested via `labelsContext` on the `drop` metric), so
  policy-denied drops are attributable per destination and the policy-deny
  alerts can watch a single namespace instead of node-wide noise (#4659)
- WireGuard encrypts pod-to-pod traffic transparently at the same layer
- **Configuration:** `projects/platform/cilium/values.yaml`

## Observable by Default Philosophy

- New deployments → Get OTEL env vars (Kyverno); network metrics come from the CNI
- Namespaces opted into OTel Operator → Also get language-level SDK injection
- Existing deployments → Get annotations/vars via background policies
- **Opt-out if needed** (see below)

## Opting Out

### Opt-out of OTEL injection

```yaml
metadata:
  labels:
    otel.instrumentation: "disabled"
```

## Configuration

- OTEL: `projects/platform/kyverno/values.yaml` (otelInjection section)
- Hubble/Cilium: `projects/platform/cilium/values.yaml`

## Excluded Namespaces (Kyverno policies)

- System: kube-system, kube-public, kube-node-lease
- Infrastructure: cert-manager, kyverno, argocd, longhorn-system, signoz, opentelemetry-operator

## Service Requirements

Every service must:

- [ ] Export Prometheus metrics on `/metrics`
- [ ] Provide health check endpoint
- [ ] Send structured logs
- [ ] Include OpenTelemetry tracing (for user-facing services)
