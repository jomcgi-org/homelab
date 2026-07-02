# Observability Architecture

This document describes the automatic observability setup in the cluster.

## Overview

Every service gets automatic observability through three layers:

1. **OTEL Environment Variables** (Kyverno) - Endpoint configuration for all workloads
2. **OpenTelemetry Operator** - Language-specific auto-instrumentation (Go, Python, Node.js)
3. **Linkerd Service Mesh** - Infrastructure-level distributed tracing and mTLS

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
│  ┌──────────────────────────┐  ┌───────────────────────────────┐    │
│  │  OTEL Injection Policy   │  │  Linkerd Injection Policy     │    │
│  ├──────────────────────────┤  ├───────────────────────────────┤    │
│  │ Adds env vars:           │  │ Adds namespace annotation:    │    │
│  │ - OTEL_EXPORTER_         │  │   linkerd.io/inject=enabled   │    │
│  │   OTLP_ENDPOINT          │  │                               │    │
│  │ - OTEL_EXPORTER_         │  │ (applies to namespace,        │    │
│  │   OTLP_PROTOCOL=grpc     │  │  affects all pods in it)      │    │
│  └──────────────────────────┘  └───────────────────────────────┘    │
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
│                   Layer 3: Linkerd Proxy Injection                  │
├─────────────────────────────────────────────────────────────────────┤
│  Linkerd webhook sees namespace annotation and injects:             │
│  - linkerd-proxy sidecar container                                  │
│  - init container for iptables rules                                │
│  - Additional annotations and labels                                │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Running Pod                                 │
├─────────────────────────────────────────────────────────────────────┤
│  ┌────────────────────┐          ┌──────────────────────────────┐   │
│  │  Application       │          │  linkerd-proxy sidecar       │   │
│  │  Container         │◄────────►│  (intercepts all traffic)    │   │
│  ├────────────────────┤          ├──────────────────────────────┤   │
│  │ OTEL env vars set  │          │ Sends traces to SigNoz       │   │
│  │ OTel SDK injected  │          │ via control plane            │   │
│  │ (if namespace opted│          │                              │   │
│  │  into Operator)    │          │                              │   │
│  └────────────────────┘          └──────────────────────────────┘   │
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
- **Go:** eBPF-based — no code changes needed, instruments at the kernel level
- **Python:** Injects `autoinstrumentation-python` init container that patches the runtime
- **Node.js:** Injects `autoinstrumentation-nodejs` init container with require hooks
- Kyverno sets the OTEL endpoint; the Operator provides the SDK — they complement each other
- **Configuration:** `projects/platform/opentelemetry-operator/` with namespace list in values

### 3. Linkerd Service Mesh (Infrastructure-Level)

- **All namespaces** automatically get `linkerd.io/inject=enabled`
- Linkerd webhook injects sidecars into all pods
- Captures ALL HTTP/HTTPS traffic (no SDK needed!)
- Automatic distributed tracing for everything
- **Policy:** `projects/platform/kyverno/templates/linkerd-injection-policy.yaml`

## Observable by Default Philosophy

- New deployments → Get OTEL env vars (Kyverno) + Linkerd sidecar
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

### Opt-out of Linkerd injection

```yaml
# Namespace level
apiVersion: v1
kind: Namespace
metadata:
  name: my-namespace
  labels:
    linkerd.io/inject: "disabled"
```

## Configuration

- OTEL: `projects/platform/kyverno/values.yaml` (otelInjection section)
- Linkerd: `projects/platform/kyverno/values.yaml` (linkerdInjection section)

## Excluded Namespaces (Kyverno policies)

- System: kube-system, kube-public, kube-node-lease
- Infrastructure: linkerd, cert-manager, kyverno, argocd, longhorn-system, signoz, opentelemetry-operator

## Service Requirements

Every service must:

- [ ] Export Prometheus metrics on `/metrics`
- [ ] Provide health check endpoint
- [ ] Send structured logs
- [ ] Include OpenTelemetry tracing (for user-facing services)
