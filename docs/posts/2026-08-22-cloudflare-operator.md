---
title: Cloudflare Operator
date: 2026-08-22
summary: Annotate a Deployment and get DNS, a Zero Trust app, and tunnel routing provisioned automatically.
public: false
---

Every new service meant clicking through the Cloudflare dashboard: create a DNS record, create a Zero Trust application, update the tunnel config. I wanted to annotate a Deployment and have everything provisioned automatically.

## How it works

**Annotation-driven.** cloudflare.ingress.hostname and cloudflare.zero-trust.policy annotations trigger reconciliation. No CRDs to manage for the common case.

**State machine.** Built with Sextant. Pending to CreatingDNS to CreatingZTApp to UpdatingConfig to Ready, each step idempotent.

**Finalizers.** Deleting the Deployment cleans up DNS records, Zero Trust apps, and tunnel routes. No orphaned Cloudflare resources.

**Drift detection.** Periodic reconciliation reverts manual dashboard edits. The operator is the source of truth.

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
