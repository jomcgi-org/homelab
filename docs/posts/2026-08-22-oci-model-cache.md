---
title: OCI Model Cache Operator
date: 2026-08-22
summary: Reference a HuggingFace model in a pod spec like a container image; the operator caches it in an OCI registry and rewrites the pod at admission.
public: false
---

HuggingFace models are huge and slow to download. I wanted to reference a model in a pod spec the same way you reference a container image, and have it just work. The operator caches models in an OCI registry and streams them to pods without touching disk.

## How it works

**PodMutator.** An admission webhook intercepts pods with hf.co/ volume references, resolves the OCI ref synchronously (pod specs are immutable after admission), creates a ModelCache CR, and gates scheduling until the model is synced.

**State machine.** Built with Sextant: Pending, Resolving, Syncing, Ready, with a Failed state. Guards distinguish permanent errors from transient failures for automatic retry.

**hf2oci.** Streams HuggingFace models into OCI layers: HTTP response to tar to io.Pipe to registry push. Zero disk I/O. Safetensors and GGUF formats.

**Deduplication.** The HuggingFace baseModels API resolves derivative models to their base, so derivatives share OCI layers with the base repo.

## Source

- [projects/operators/oci-model-cache](https://github.com/jomcgi/homelab/tree/main/projects/operators/oci-model-cache)

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
