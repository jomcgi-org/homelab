# OCI Model Cache Operator

Custom Kubernetes operator that syncs ML models from HuggingFace to OCI registries.

## Overview

Uses a `ModelCache` CRD to declaratively manage model caching. Compiler-enforced state machine transitions with sealed interfaces and OpenTelemetry tracing baked into every phase change.

| Package                   | Description                                                                                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **cmd**                   | Operator entrypoint                                                                                                                                          |
| **api**                   | CRD type definitions (`ModelCache`)                                                                                                                          |
| **internal/controller**   | `ModelCache` reconciler: sync job building, pod scheduling-gate removal, cleanup, and resolver logic                                                         |
| **internal/config**       | Operator configuration from flags and environment variables (registry, default TTL, copy image, etc.)                                                        |
| **internal/hfref**        | Parses `hf.co/{org}/{model}` pod volume names into HuggingFace repo + optional GGUF file selector                                                            |
| **internal/naming**       | Derives deterministic, DNS-safe Kubernetes resource names from a HuggingFace repo/file pair                                                                  |
| **internal/statemachine** | Sealed-interface state machine for phase transitions                                                                                                         |
| **internal/telemetry**    | OpenTelemetry tracing setup shared across controller and webhook                                                                                             |
| **internal/webhook**      | Pod-mutating admission webhook: rewrites `hf.co/` volume refs to OCI refs, creates `ModelCache` CRs on demand, and gates scheduling until the cache is ready |
| **helm**                  | Helm chart for operator deployment                                                                                                                           |
