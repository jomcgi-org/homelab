The factory tunnel Gateway and its Secrets live in `cfgate-system`.
cfgate v0.2.0-alpha.11 starts a cluster-wide Secret informer. The factory
source patch `secret-cache.patch` scopes that informer to `cfgate-system`.
It leaves Gateway and HTTPRoute watches at their existing scope.

Apply the patch with `git apply` in a cfgate checkout at upstream commit
`d5bebc42cc27f606ca84aa3d3681071e5f991fcd` before building the controller
image. Run `go test ./cmd/manager ./internal/controller` in that checkout.
Once the patched image is available, pin its digest in the overlay and
remove the Secret rule from the ClusterRole. Update the rendered RBAC test
to expect Secret grants only in `cfgate-system`. After sync, check the
controller's readiness, tunnel and route status, and the ServiceAccount's
all-namespace Secret denial.
