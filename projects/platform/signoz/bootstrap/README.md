# signoz/bootstrap

Out-of-band node configuration that turns on **Kubernetes system traces** for
kube-apiserver and kubelet, so control plane internals show up in SigNoz
alongside application traces. Applied by hand, not through ArgoCD.

Closes the one visibility gap in the observability stack: we have application
traces from the OTEL SDKs and network telemetry from Cilium/Hubble, but nothing
from Kubernetes itself. With this enabled, SigNoz shows the API request
lifecycle (authn, authz, admission webhooks, etcd), webhook latency including
Kyverno policy evaluation as child spans, and kubelet CRI calls to containerd.

## Why this exists (and why not GitOps)

ArgoCD reconciles Kubernetes objects. It cannot edit a node's
`/etc/rancher/k3s/config.yaml`, write into the kubelet config drop-in directory,
or restart k3s. Tracing for the API server and kubelet is configured *before*
those components start, so it is necessarily node-level state.

There is also a bootstrap ordering point: the apiserver is the first thing up on
a server node, long before CoreDNS can resolve anything. That is why the OTLP
endpoint is `localhost:4317` rather than a Service DNS name, and why the
observability path must not route through cluster networking.

## Prerequisite: the agent must be on the host network

`localhost:4317` only exists because `k8s-infra.otelAgent.hostNetwork: true` is
set in `../values-prod.yaml`. **Land and sync that change before applying
anything here.**

The k8s-infra chart declares `hostPort: 4317` on the agent, and it looks
plausible in `kubectl get ds`, but it never bound anything on these nodes:

- The active CNI config is `05-cilium.conflist`, whose plugin list is
  `cilium-cni` alone, with no chained `portmap` plugin. (A `10-flannel.conflist`
  with portmap still sits beside it, left over from before the Cilium migration
  in ADR platform/012, but it sorts second and is dead.)
- `cilium-config` has `kube-proxy-replacement: false`, so Cilium's own eBPF
  hostPort path is off too.

So `hostPort` is a no-op cluster-wide. Nothing noticed because every other
apparent hostPort user (`cilium`, `cilium-envoy`, `cilium-operator`) is
`hostNetwork: true` and never depended on the mapping. Verify before proceeding:

```bash
# On each node. Must print OPEN.
bash -c 'exec 3<>/dev/tcp/127.0.0.1/4317' && echo OPEN || echo CLOSED
```

`hostNetwork` also requires `dnsPolicy: ClusterFirstWithHostNet`, which the
chart cannot express; the `otel-agent-hostnet-dns` Kyverno policy supplies it.
See `../../kyverno/templates/otel-agent-hostnet-dns-policy.yaml`.

## Files

| File              | Installed to                                                    | Applied on   | Purpose                                     |
| ----------------- | --------------------------------------------------------------- | ------------ | ------------------------------------------- |
| `tracing.yaml`    | `/etc/rancher/k3s/tracing.yaml`                                  | node-1/2/3   | apiserver `TracingConfiguration`            |
| `k3s-config.yaml` | append to `/etc/rancher/k3s/config.yaml`                         | node-1/2/3   | points apiserver at the file above          |
| `10-tracing.conf` | `/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/10-tracing.conf`  | all 4 nodes  | kubelet tracing via `KubeletConfiguration`  |

node-4 is a worker with no apiserver, so it takes only `10-tracing.conf`.

### The kubelet is not configured the way the apiserver is

Worth stating plainly, because the obvious symmetry is wrong and the failure
mode is severe. `kube-apiserver` has a `--tracing-config-file` flag. **The
kubelet does not.** Its `TracingConfiguration` is an embedded field of
`KubeletConfiguration`, so it has to arrive through the config drop-in
directory.

Passing `tracing-config-file` via k3s `kubelet-arg` would be an unknown flag.
The kubelet would refuse to start and take the node with it, and on a server
node that means an etcd member. This cluster has already lost all three masters
once to exactly that mistake with a bad `image-minimum-gc-age` kubelet flag.

## Apply (during a maintenance window)

Server nodes **one at a time**, waiting for Ready and etcd quorum between each.
Never restart k3s on more than one master at once.

1. Install the config files:

   ```bash
   sudo install -m 0644 tracing.yaml /etc/rancher/k3s/tracing.yaml
   sudo cp /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.bak
   sudo tee -a /etc/rancher/k3s/config.yaml < k3s-config.yaml
   sudo install -m 0600 10-tracing.conf \
     /var/lib/rancher/k3s/agent/etc/kubelet.conf.d/10-tracing.conf
   ```

2. Restart k3s and wait for the node to come back:

   ```bash
   sudo systemctl restart k3s
   kubectl wait --for=condition=Ready node/<node> --timeout=300s
   kubectl get --raw /healthz            # apiserver
   kubectl -n kube-system exec <etcd-pod> -- etcdctl endpoint health   # quorum
   ```

3. Only once the node is Ready and quorum is intact, move to the next.

node-4 takes step 1's `10-tracing.conf` line only, then
`sudo systemctl restart k3s-agent`.

### Rollback

Remove the appended `kube-apiserver-arg` block (or restore `config.yaml.bak`),
delete `10-tracing.conf`, and restart k3s. Tracing is additive and carries no
state, so rollback is a clean revert with no migration.

If a node fails to start after the change, k3s logs the rejected flag:

```bash
sudo journalctl -u k3s -n 100 --no-pager | grep -i "unknown\|invalid\|tracing"
```

## Verify

```bash
# Spans arriving at the agent
kubectl logs -n signoz -l app.kubernetes.io/component=otel-agent --tail=50 | grep -i trace
```

In SigNoz, the services appear as `kube-apiserver` and `kubelet`. Absence of an
error is not evidence of success here: an unreachable OTLP endpoint makes the
apiserver drop spans without failing, which is the exact trap this directory's
prerequisite section exists to prevent.

## Follow-up

- Watch otel-agent CPU and memory for a few days. At 0.1% sampling the overhead
  should be negligible, but the agent is capped at 500m/500Mi.
- Raise `samplingRatePerMillion` in both `tracing.yaml` and `10-tracing.conf`
  together if more coverage is wanted for a specific investigation.
- The root cause behind the dead `hostPort` is that Cilium runs with
  `kubeProxyReplacement: false`. Enabling it would make hostPort work
  cluster-wide and remove the need for `hostNetwork` here, but that is a live
  datapath change deserving its own ADR and window.
