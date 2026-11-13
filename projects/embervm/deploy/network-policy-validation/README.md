# Validate noded ingress isolation

Repository support for #7007 is staged off. The authorized validation owner
activates `noded.ingressNetworkPolicy.enabled` through GitOps in embervm-dev
first. Production activation follows recorded dev acceptance. Disable the same
flag through GitOps to roll back. Keep bearer and SPIFFE authentication enabled
as configured. This procedure does not authorize workload operations or probes.

## Caller decisions

The policy selects the release-derived noded app name and instance, with
component `noded` or `noded-brick`. Class and floor labels remain unchanged.
Pod selectors in each ingress peer implicitly require the policy's namespace.
There are no cross-namespace grants, egress rules, Cilium resources or L7 rules.

| Destination on noded pod IP | Allowed pod source in the same release and namespace | Source of decision |
| --- | --- | --- |
| `noded.grpcPort`, default 9090, only with `plaintextGrpc.enabled` | Control plane, app name and instance from `embervm.selectorLabels` | `_noded-pod.tpl`, `deployment.yaml`, CP node registry gRPC client |
| `noded.spiffe.grpcTlsPort`, default 9443, only with `spiffe.enabled` | Same control plane | `_noded-pod.tpl`; production `deploy/values-gke.yaml` enables the second listener |
| `noded.activatorPort`, default 8081 | Serving Envoy, its app name, instance and component | `serving-envoy-daemonset.yaml`, xDS serving clusters, `noded/server/activator.go` |
| `servingEnvoy.statefulTcpPortRange` and `compositeTcpPortRange`, default 5400-5419 | Serving Envoy | `_noded-pod.tpl`, `noded/server/stateful_activator.go` and `group_activator.go`; stale local ownership forwards to CP |
| Warm HTTP/stateful/composite pod-IP DNAT endpoints | Serving Envoy and control plane | `noded/serving/net.go:PortForIP`, `group.go:EnsureEntryDNAT`, `control/lib/embervm/tcp_activator.ex` dials the published endpoint after wake |
| `noded.healthPort`, default 8080 | Kubelet through the native node-traffic exception; no pod grant | `noded/cmd/main.go:healthHandler`, `_noded-pod.tpl` HTTP probes |

The egress proxy is a container in the noded pod. Its token-broker calls are
outbound; `tokenbroker-deployment.yaml` does not call noded. It receives no
inbound grant. Same-pod sidecars and loopback cannot be isolated from noded by
native NetworkPolicy. Guest health probes originate inside noded's network
namespace and use tap addresses. Native policy does not replace the guest bridge
nftables rules. Node-origin traffic, host networking and pre-existing/additive
policies are native-policy limitations; an unrelated policy can widen access.

There is no noded metrics listener or `/metrics` handler in `healthHandler`,
no noded scrape annotation or ServiceMonitor in this chart, and no Prometheus
scraper configured on the hub (`platform/cert-manager/values.yaml`). Traces are
exported outbound to OTEL. No Prometheus namespace exception is needed. Recheck
this conclusion against live collector configuration before activation.

Warm ports are `servingPortBase + hostOffset`, rather than the guest service
port. Serving uses the daemon's fixed default `172.31.0.0/24`; composite uses
`noded.compositeSupernet`. An empty composite supernet disables composite
allocation and uses the serving /24 size for this calculation. The policy
covers base+2 through the smaller of base+subnet-size-2 and 65535, including
serving's /24. With the default /16
composite supernet and base 30000 this is 30002-65535. Offsets beyond TCP's limit
are already rejected by `PortForIP`; the policy does not make them usable.
Supported composite prefixes are /16 through /24, matching the allocator's /24
groups. Changing the serving subnet outside chart configuration is unsupported
and requires revisiting this derivation. The configured activator ranges are
separate allowances, including 5401. Rendering fails if the warm range overlaps
a noded listener, so changing the base cannot expose gRPC or health to Envoy.

GKE Dataplane V2 supports NetworkPolicy `endPort` on GKE 1.32 and later.
Earlier versions drop the field, reducing each range to its first port.
Before activation, record `kubectl version -o yaml` and
`kubectl get nodes -o wide`; require GKE 1.32 or later on the control plane
and every node hosting the tested pods. Record the hub's Dataplane V2 status.
After sync, read each installed policy with
`kubectl get networkpolicy -n <namespace> <name>-noded-ingress -o yaml` and
compare every ingress `port`/`endPort` pair with the rendered manifest,
including both warm-range grants. A missing or changed `endPort` fails the
precondition even if ArgoCD reports Synced. Record 5401 and warm ports above
the range start as positive controls.

GKE Dataplane V2 must also enforce pod ingress before the pod namespace's nftables
DNAT changes the destination to a guest tap. Service translation, pod DNAT,
forwarding and established-connection handling must be checked live. Rendering
cannot establish these packet semantics. Do not compensate with all-port or
all-namespace grants if probes fail.

## Activate the validation composition first

Checked-in embervm-dev has `servingEnvoy.enabled=false` and
`pingWorkload.enabled=false`. It renders no serving Service and no serving,
stateful or composite Workload: only the session and task lanes are present.
Checked-in production has ping, but both Postgres Workloads are disabled and
there is no composite Workload. Neither composition supplies the full probe
matrix without a reviewed activation change.

The validation owner prepares the following temporary GitOps composition for
the existing embervm-dev release and namespace, then repeats it for embervm
only after dev acceptance. Keep the environment's values and GKE overlay;
never layer production values into dev. These are activation instructions,
with no Application or checked-in overlay changed by this PR.

| Path | Activation values or Workload |
| --- | --- |
| Real Envoy and HTTP serving | Set `servingEnvoy.enabled=true` and `pingWorkload.enabled=true`. Use the chart's ping Workload and serving Service. Keep ping's dev scratch path and CI-pinned image. |
| Stateful 5401 regression | Set `demoPostgres.enabled=true`, `demoPostgres.listenPort=5401` and `demoPostgres.nodeLocalWake=true`. In dev restore `scratchPostgres.guestImage.repository=ghcr.io/jomcgi/homelab/projects/embervm/runtimes/postgres`, which the dev overlay empties. Retain its CI-pinned digest and dev scratch path. |
| Stateful 5400 control | Set `scratchPostgres.enabled=true`, `scratchPostgres.listenPort=5400` and `scratchPostgres.nodeLocalWake=true`. It shares the Postgres builder and secret with demo-postgres. |
| Composite 5410 | Add a namespace-local `network-policy-ping-group` Workload using `crd/samples/workload-composite.yaml` as the schema example and the ping substitutions below. Keep `noded.compositeSupernet` populated. |

The two Postgres Workloads require the namespace's
`<release>-scratch-postgres` Secret. The activation PR must use an already
authorized 1Password Operator item via `scratchPostgres.onepassword.itemPath`
and include that exact Secret name in `rbac.secretNames`. Dev currently has
neither binding. Verify Secret presence and the CP's named read permission
without exporting its contents. Credential creation or reuse needs the
validation owner's authorization; absence blocks functional acceptance.
Do not use an existing tenant database as a disposable test fixture.

For the composite Workload, copy the rendered ping Workload's entire
`spec.source` and `spec.resources` into the top-level seed and each member.
Replace the sample's placeholder images and members with one member named
`ping`, `replicas: 1`, `startOrder: 0` and `healthPort: 8080`. Set
`group.entry` to `{member: ping, port: 8080, listenPort: 5410}` and
`group.nodeLocalWake=true`. Retain the sample's bounded lifecycle settings;
omit `secretRef`. Use the target release's namespace. This runs the built
ping server through the composite wake and DNAT paths without new images or
an external endpoint. Commit the fully resolved Workload in the activation
PR; never deploy the sample's illustrative image names.

First sync this composition with ingress policy still off. Require ready
Envoy, ready bases, and successful functional cold and warm requests through
the serving Service for ping, both Postgres ports and the composite entry.
Record real warm pod-IP DNAT destinations for all three classes. Then enable
`noded.ingressNetworkPolicy.enabled=true` through GitOps and run the paired
probes below. Production follows the same baseline and activation sequence.

Rollback isolation by setting the policy flag false through GitOps. Roll back
the temporary composition's values and Workload registration in a separate
reviewed GitOps change after recording evidence, restoring each environment's
previous values. Preserve Postgres volumes, snapshots and Secret objects;
data deletion is outside this procedure. Remove only validation-owned
registrations after their instances have been quiesced by the authorized
owner, and keep any pre-existing production resources.

## Record bounded evidence

Retain the existing conformance runner's `/verdict` and exact chart version as
the deployment baseline (`conformance/main.go`). Its sandbox/session scenarios
do not cover serving isolation. The validation owner uses the activated
validation Workloads and serving APIs for the cold/warm operations below and
records their responses. A successful TCP handshake below establishes L4 reachability
only. Synthetic Envoy-labelled sources supply L4 controls only; functional
acceptance requires the real Envoy and active Workloads above. A handshake
does not establish a successful wake, application response, bearer check or
SPIFFE handshake.

1. Record environment, delivered chart/revision, policy manifest, matching pod
   names/UIDs/IPs and labels, namespace labels, other policies, and listener
   configuration. Verify ArgoCD Synced/Healthy with `verify_deployment`. Inspect
   all legacy, class and floor shapes present in that release. Compare actual
   composition to the GKE Applications: prod loads `deploy/values.yaml` then
   `deploy/values-gke.yaml`; dev loads `dev/deploy/values.yaml` then its GKE file.
2. The validation owner supplies an allowed source with the real CP labels for
   gRPC and warm TCP fallback, and one with serving Envoy labels for HTTP,
   stateful/composite and warm serving. Use existing owner-managed probe
   execution infrastructure; add no broad RBAC. Record the source UID, labels,
   namespace and node. Supply an unlisted namespace source with no allowed
   namespace membership. Avoid hostNetwork and the destination node for the
   negative source so the node exception cannot masquerade as a pod grant.
3. Prepare the validation Workloads using the Workload/serving APIs.
   List exact pod IP/port pairs for configured plaintext/TLS listeners, HTTP
   activator, stateful 5400 and 5401, composite 5410, and one warm serving endpoint
   for each serving kind. For overridden ranges use their active equivalents;
   separately retain the default 5401 regression. Preserve workload state during
   each comparison. Missing optional listeners are recorded as not applicable,
   never as denied. A listener must have a successful allowed-source control.
4. From each source, run the following credential-free Python socket check
   against the *same* IP/port pairs. Pass numeric pod IPs only. Bound the target
   list to 32 entries, each with a 3-second timeout, and the whole invocation to
   110 seconds in the owner's execution harness. Save stdout, exit code and
   timestamps. Run allowed, negative, allowed again to bracket listener activity.

   ```python
   import json, socket, sys, time
   # python probe.py allowed|denied POD_IP:PORT ...
   mode, *targets = sys.argv[1:]
   assert mode in {"allowed", "denied"} and 0 < len(targets) <= 32
   passed = True
   for target in targets:
       host, port = target.rsplit(":", 1)
       socket.inet_pton(socket.AF_INET, host)
       start = time.monotonic()
       try:
           with socket.create_connection((host, int(port)), timeout=3):
               result = "connected"
       except TimeoutError:
           result = "timeout"
       except OSError as error:
           result = "error:" + str(error.errno)
       ok = result == ("connected" if mode == "allowed" else "timeout")
       passed &= ok
       print(json.dumps(dict(target=target, result=result, passed=ok,
                             elapsed=time.monotonic()-start)), flush=True)
   sys.exit(0 if passed else 1)
   ```

5. Pair the three reports by destination and port. A denied result passes only
   with both successful positive controls and a timeout from the unlisted
   namespace. Refusal, absent listeners, route errors and failed controls are
   inconclusive and fail acceptance. Recreate connections after policy
   activation; pre-existing connections do not prove new-flow enforcement.
   Health is a separate control: kubelet readiness must remain successful while
   pod-source health connections are denied. Verify local egress sidecar/broker
   operation and any discovered scrape path against the matrix.
6. Record functional API results for HTTP/stateful/composite cold wakes,
   including 5401, then repeat on warm endpoints. Retain active warm destinations
   for the paired L4 negative checks. Record TLS functionality through the
   existing authenticated CP path without copying credentials into probes.
   Fail on any lost path or an unexpected successful negative connection.

Settle each run within 72 hours. Dev must pass before production activation.
After both pass, attach revisions, destinations and reports to #7007. Only then
change the authored STPA native-isolation row to `enforced-prod`, regenerate
STPA.md, retire its open question and remove the Direction entries. This PR
supplies no live enforcement evidence.
