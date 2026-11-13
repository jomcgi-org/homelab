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
`noded.compositeSupernet`. The policy covers base+2 through the smaller of
base+subnet-size-2 and 65535, including serving's /24. With the default /16
composite supernet and base 30000 this is 30002-65535. Offsets beyond TCP's limit
are already rejected by `PortForIP`; the policy does not make them usable.
Supported composite prefixes are /16 through /24, matching the allocator's /24
groups. Changing the serving subnet outside chart configuration is unsupported
and requires revisiting this derivation. The configured activator ranges are
separate allowances, including 5401.

GKE Dataplane V2 must enforce pod ingress before the pod namespace's nftables
DNAT changes the destination to a guest tap. Service translation, pod DNAT,
forwarding and established-connection handling must be checked live. Rendering
cannot establish these packet semantics. Do not compensate with all-port or
all-namespace grants if probes fail.

## Record bounded evidence

Retain the existing conformance runner's `/verdict` and exact chart version as
the deployment baseline (`conformance/main.go`). Its sandbox/session scenarios
do not cover serving isolation. The validation owner uses the existing Workload
CRs and serving APIs for the additional cold/warm operations below and records
their responses. A successful TCP handshake below establishes L4 reachability
only. It does not establish a successful wake, application response, bearer
check or SPIFFE handshake.

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
3. Prepare known-active workloads using the existing Workload/serving APIs.
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
