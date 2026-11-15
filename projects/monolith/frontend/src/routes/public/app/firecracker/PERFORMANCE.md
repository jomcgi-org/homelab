# Firecracker performance

`/app/firecracker` sends a permanent 301 redirect to `/ember/firecracker`.
The destination is a server-rendered explainer with no data endpoint.

## Cache windows

The redirect loader and destination have no explicit shared-cache TTL, SWR or
SIE. The pending [response-boundary change](https://github.com/jomcgi-org/homelab/pull/7967)
marks the app redirect `no-store`; the Ember destination remains outside that
app-specific opt-in rule. No configured maximum cache age can be inferred for
it from these files. Static-page caching remains in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

Content changes through commits and deployment. The redirect route's emitted
JavaScript budget does not imply navigation downloads that route's page bundle;
measure the redirect and destination separately.

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/firecracker` | 65,000 | 0 | 0 |
| `/ember/firecracker` | 85,000 | 0 | 0 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
