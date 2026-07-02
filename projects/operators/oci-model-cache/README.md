# OCI Model Cache Operator

Custom Kubernetes operator that syncs ML models from HuggingFace to OCI registries.

## Overview

Uses a `ModelCache` CRD to declaratively manage model caching. Compiler-enforced state machine transitions with sealed interfaces and OpenTelemetry tracing baked into every phase change.

| Package                   | Description                                          |
| ------------------------- | ---------------------------------------------------- |
| **cmd**                   | Operator entrypoint                                  |
| **api**                   | CRD type definitions (`ModelCache`)                  |
| **internal**              | Controller reconciliation logic                      |
| **internal/statemachine** | Sealed-interface state machine for phase transitions |
| **helm**                  | Helm chart for operator deployment                   |
