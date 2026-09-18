# Monolith public

`monolith-public` is the separate public composition for `jomcgi.dev`. It uses
the pruned public backend entry point and the public frontend image produced by
`projects/monolith`, with its own chart, values, routes, secrets, and database
roles.

## Fail-closed chart boundary

The public chart is intentionally separate from
[`../monolith/chart`](../monolith/chart). A template added to the private
monolith chart has no effect on this tier. A public feature needs an explicit
template or value in this directory, a route in
[`chart/templates/httproute-public.yaml`](chart/templates/httproute-public.yaml),
and any required public-binary import or database grant changes in
`projects/monolith`.

Guards in `projects/monolith` read this chart and assert several parts of that
boundary: the public route omits private API and chat paths, Turnstile secrets
stay in the backend, packaged library images use content digests, and scoped
egress templates retain their declared destinations.

## Layout and delivery

```text
projects/monolith-public/
├── chart/                 public Helm chart and vendored dependencies
└── deploy/                ArgoCD Application and environment values
```

[`deploy/application.yaml`](deploy/application.yaml) selects the published
`monolith-public` chart and reads environment values from Git. The chart build
pins backend and frontend images from their Bazel image providers. The GKE
Application under [`../gke-apps/monolith-public`](../gke-apps/monolith-public/)
uses the same chart with `deploy/values.yaml` and `deploy/values-gke.yaml`.

The chart declares `OnePasswordItem` resources for its credentials. The public
reader and writer roles are defined by monolith database migrations and their
grants. No credential value belongs in this directory.

## Security scope

The public HTTPRoute lists each internet-facing path explicitly. Backend reads
use the public database role, while public chat uses its constrained writer
path and Turnstile admission. Public responses and routes must follow
[`../../docs/runbooks/public-tier-checklist.md`](../../docs/runbooks/public-tier-checklist.md).

The chart also contains Cilium policy templates. The checked-in GKE overlay
disables those resources because the target cluster does not install the
Cilium CRDs. A rendered policy is repository intent, not evidence of live
enforcement.
