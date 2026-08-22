---
title: rules_semgrep
date: 2026-08-22
summary: Hermetic Semgrep static and supply-chain analysis as Bazel tests: digest-pinned OCaml engine, cached diff scans in 30 seconds.
public: false
---

Semgrep on managed CI took 2+ minutes per diff scan and rule-registry fetches made results non-deterministic. I needed scans that run in seconds, produce identical results from identical inputs, and only re-run when something changed. Bazel's content-addressed cache gives all three, but Semgrep had no Bazel integration.

## How it works

**No Python.** Extracts the semgrep-core OCaml binary from PyPI wheels and vendors it as an OCI artifact on GHCR, bypassing the Python wrapper and its startup tax.

**Digest-pinned.** Engine binaries and Pro rule packs are pinned to sha256 digests; a daily job updates digests and opens a PR. Same inputs, same results.

**Three rule types.** semgrep_test for sources, semgrep_manifest_test for Helm-rendered YAML, semgrep_target_test for transitive deps via aspect. Gazelle generates all of them.

**Supply chain.** SCA lockfile scanning with Pro reachability, auto-detected from @pip and @npm dependency prefixes. Zero config.

**Results.** Cached diff scans in 30 seconds, down from 2+ minutes. Cold cache: 4 minutes for all tests, images, and scans.

## Source

- [bazel/semgrep](https://github.com/jomcgi/homelab/tree/main/bazel/semgrep)

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
