---
title: Formal verification at home
date: 2026-10-02
tags: embervm, tla, conformance, gitops, homelab
public: true
summary: Trace-based conformance testing for EmberVM, replayed from one real run.
---

EmberVM is a complex Elixir operator that manages horizontally scalable VMs on K8s, allowing you to run durable agents, FaaS and funky Firecracker-sandboxed tools.

This is a nightmare to manage. To make some of that easier I've used TLA+ to model the scenarios that were frequently breaking and then introduced trace-based conformance testing.

So any time my agents merge a change we build and deploy the service to dev in my k8s cluster and [Kargo](https://kargo.io/) executes this test asserting compliance before we promote the change to production.
