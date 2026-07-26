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

The k8s-infra chart declares `hostPort: 4317` on the agent, and it looked
plausible in `kubectl get ds`, but at the time this was written it never bound
anything, because the active CNI config (`05-cilium.conflist`) chains no
`portmap` plugin and Cilium ran with `kube-proxy-replacement: false`, which
disables its own eBPF hostPort path. Every other apparent hostPort user
(`cilium`, `cilium-envoy`, `cilium-operator`) is `hostNetwork: true` and never
depended on the mapping, so nothing noticed.

`kubeProxyReplacement` is now **true** (issue #3823), so Cilium's eBPF hostPort
path is live. That does **not** mean this directory's `hostNetwork` approach can
be reverted: Cilium programs hostPort frontends on node addresses, and
`cilium-dbg service list` shows **zero `127.0.0.1` frontends**. Everything here
points at `localhost:4317`, so switching back to `hostPort` would likely break
tracing silently. Prove loopback works before touching it.

Verify before proceeding. Must print OPEN on every node:

```bash
bash -c 'exec 3<>/dev/tcp/127.0.0.1/4317' && echo OPEN || echo CLOSED
```

### Also check for orphaned DNAT rules on the OTLP port

Stale flannel-era `portmap` chains were found on node-1 and node-4 redirecting
4317/4318/8888/13133 to pods deleted long ago. A socket LISTENs while every
connect times out, so the apiserver exports into a blackhole with no error. Must
be 0 on every node:

```bash
sudo iptables -t nat -S | grep -c '^-N CNI-DN-'
```

The diagnostic tell if you hit it: a **timeout** means a DNAT blackhole, an
instant (~0.07ms) **connection refused** means simply no listener. Those are
different faults that look identical in a probe result.

`hostNetwork` also requires `dnsPolicy: ClusterFirstWithHostNet`, which the
chart cannot express; the `otel-agent-hostnet-dns` Kyverno policy supplies it.
See `../../kyverno/templates/otel-agent-hostnet-dns-policy.yaml`.

## Files

| File              | Installed to                                | Applied on  | Purpose                                    |
| ----------------- | ------------------------------------------- | ----------- | ------------------------------------------ |
| `tracing.yaml`    | `/etc/rancher/k3s/tracing.yaml`             | node-1/2/3  | apiserver `TracingConfiguration`           |
| `k3s-config.yaml` | append to `/etc/rancher/k3s/config.yaml`    | node-1/2/3  | points apiserver at the file above         |
| `10-tracing.conf` | `<kubelet config-dir>/10-tracing.conf`      | all 4 nodes | kubelet tracing via `KubeletConfiguration` |

node-4 is a worker with no apiserver, so it takes only `10-tracing.conf`.

### The kubelet config-dir is per-node. Do not hardcode it.

`/var/lib/rancher/k3s/agent/etc/kubelet.conf.d` is correct for node-1/2/3 but
**wrong for node-4**, which runs a custom k3s `--data-dir` on its NVMe disk
(`/disks/nvme-01/k3s-data/...`).

This is worse than a wrong path, because a **stale copy of the default tree
still exists on node-4**, complete with a plausible-looking
`00-k3s-defaults.conf` from before the move. Installing into it succeeds, the
file is present with the right mode and owner, k3s restarts clean, nothing
errors, and the feature is completely inert. Derive the directory instead:

```bash
D=$(journalctl -u k3s-agent -u k3s --no-pager \
      | grep -oE '[-]-config-dir=[^ ]+' | tail -1 | cut -d= -f2)
sudo install -m 0600 10-tracing.conf "$D/10-tracing.conf"
```

k3s only ever rewrites its own `00-k3s-defaults.conf` in that directory
(`writeKubeletConfig` in `pkg/daemons/agent/agent.go`; `pkg/agent/config/config.go`
merely `MkdirAll`s it), so a `10-` prefixed file both sorts after the defaults
and survives restarts and upgrades.

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
   # Guard: appending a second kube-apiserver-arg key would be a duplicate YAML
   # mapping key and would silently drop one of the two lists.
   grep -q '^kube-apiserver-arg:' /etc/rancher/k3s/config.yaml && echo "ALREADY SET, stop"

   sudo install -m 0644 tracing.yaml /etc/rancher/k3s/tracing.yaml
   sudo cp /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.bak
   sudo tee -a /etc/rancher/k3s/config.yaml < k3s-config.yaml

   D=$(journalctl -u k3s-agent -u k3s --no-pager \
         | grep -oE '[-]-config-dir=[^ ]+' | tail -1 | cut -d= -f2)
   sudo install -m 0600 10-tracing.conf "$D/10-tracing.conf"

   # Confirm both lists survived the append.
   python3 -c "import yaml;d=yaml.safe_load(open('/etc/rancher/k3s/config.yaml'));\
print('apiserver:',d.get('kube-apiserver-arg'),'kubelet-arg:',len(d.get('kubelet-arg',[])))"
   ```

   Run these over `ssh` by copying a script and executing it (`scp x.sh node:/tmp
   && ssh node 'sudo -S -p "" bash /tmp/x.sh' < pwfile`) rather than as an inline
   quoted command. At least one node has **fish** as the login shell, which
   mangles `$` in inline `sed`/`bash -c` strings.

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

Every check in this section exists because a weaker one passed while the feature
was inert. Do not substitute "the file is there and k3s restarted clean".

**1. Check the kubelet's own running state, not the file.** This is the only
thing that distinguishes applied from looks-applied:

```bash
kubectl get --raw /api/v1/nodes/<node>/proxy/configz \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['kubeletconfig'].get('tracing'))"
```

Expected: `{'endpoint': 'localhost:4317', 'samplingRatePerMillion': 1000}`.
`None` means the drop-in went somewhere the kubelet does not read. Right after a
restart the endpoint returns empty for a few seconds; retry before concluding.

**2. Confirm the apiserver actually took the flag** (it is not introspectable any
other way: `k3s kube-apiserver --help` is not a subcommand and the kubelet runs
in-process):

```bash
sudo journalctl -u k3s --no-pager | grep -oE '[-]-tracing-config-file=[^ "]+' | tail -1
```

**3. Count spans by transport.** `grpc` is the control plane, `http` is app
workloads, so a raw total proves nothing:

```bash
sudo curl -sS http://127.0.0.1:8888/metrics \
  | grep -E '^otelcol_(receiver_accepted|exporter_sent|exporter_send_failed)_spans'
```

**An idle apiserver is indistinguishable from a broken one.** At 0.1% sampling a
master serving little traffic emits zero spans, which looks exactly like a
blackholed endpoint. Drive load first, then count:

```bash
for i in $(seq 1 1200); do kubectl --server=https://<node-ip>:6443 get --raw /version >/dev/null; done
```

**4. To prove the path end to end**, temporarily set
`samplingRatePerMillion: 1000000` in the drop-in, restart, confirm a `grpc`
series appears, then revert and restart again. This is the only cheap way to
distinguish "correctly configured but sparse" from "silently broken".

In SigNoz the services appear as `kube-apiserver` and `kubelet`. Absence of an
error is not evidence of success: an unreachable OTLP endpoint makes the
apiserver drop spans without failing.

## Follow-up

- **`samplingRatePerMillion` is not a hard volume cap.** It bounds only traces
  the apiserver *starts*. A request arriving with an already-sampled
  `traceparent` is exported regardless of the rate, and this cluster's workloads
  are OTEL-instrumented and propagate it. Observed span volume ran well above
  what 0.1% of request count predicts, so treat the rate as a floor, not a
  ceiling.
- Because of the above, watch otel-agent CPU and memory rather than assuming
  0.1% makes it free. The agent is capped at 500m/500Mi.
- Raise `samplingRatePerMillion` in both `tracing.yaml` and `10-tracing.conf`
  together if more coverage is wanted for a specific investigation.
- Control-plane coverage is **per-apiserver**: each master traces only the
  requests it serves, so what lands in SigNoz is weighted by how clients
  distribute. In-cluster clients spread across all three via the `kubernetes`
  Service; external `kubectl` follows whatever its kubeconfig names.
- `kubeProxyReplacement` is now enabled (#3823), so Cilium's eBPF hostPort path
  is live. Reverting `hostNetwork` here is **not** a simple follow-up: Cilium
  programs hostPort frontends on node addresses and currently exposes no
  `127.0.0.1` frontend, so it would likely break `localhost:4317` silently.
  Prove loopback reachability first.
