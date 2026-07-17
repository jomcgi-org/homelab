# R5 Task 10 scratch-k8s composite live drill (POST-MERGE)

The acceptance drill for the `scratch-k8s` composite consumer (plan Task 10). It is
run POST-MERGE against the deployed cluster; the controller fills the TODO
placeholders into the PR description. **Nothing here is fabricated** until the drill
runs.

Scope: an agent `run_python` session (the wired consumer,
`projects/monolith/sandbox/client.py`) `kubectl`s against the scale-to-zero scratch
Kubernetes cluster, deploys a pod, lets the group bank, later wakes it, and finds
the pod (a warm relight preserved state) OR finds it gone (a fresh boot: the
warmth-only contract held). Both outcomes are recorded.

## Prerequisites

- The `scratch-k8s` Workload CR SYNCED (embervm chart `scratchK8s.enabled: true`
  with the `scratch-k8s` 1Password item present), the two base rootfs builds
  complete (`build-scratch-k8s-server-rootfs` / `build-scratch-k8s-agent-rootfs`
  initContainers), and the composite dispatcher priming the group on first connect.
- The monolith wired (`scratchK8s.enabled: true`): `SCRATCH_K8S_KUBECONFIG` present
  in the monolith deployment env, so a `run_python` snippet gets `KUBECONFIG` set.
- The `scratch-k8s` 1Password item's `EMBER_GROUP_SECRET` field synced into BOTH
  the embervm and the monolith namespace Secrets.

## Step 1: agent session reaches the cluster (three Ready nodes)

From an agent `run_python` session (KUBECONFIG is pre-set by the wiring), the first
`kubectl` wakes the group (cold boot: server then two agents). Record wall-clock
from the first connect to the `kubectl get nodes` response and the per-member clock
deltas noded logs.

```python
import subprocess
print(subprocess.run(["kubectl", "get", "nodes", "-o", "wide"],
                     capture_output=True, text=True).stdout)
```

`kubectl get nodes` output (drill):

```
TODO: paste `kubectl get nodes -o wide` here (must show 3 nodes Ready:
the server + agent-0 + agent-1)
```

## Step 2: deploy a pod

```python
import subprocess
subprocess.run(["kubectl", "run", "drill-probe", "--image=<airgap-image>",
                "--restart=Never"], check=True)
# wait for Running, then:
print(subprocess.run(["kubectl", "get", "pod", "drill-probe", "-o", "wide"],
                     capture_output=True, text=True).stdout)
```

Record: pod reached Running (yes/no), and the image used (must be an airgap-staged
image so the deploy needs no egress).

## Step 3: let the group bank, then wake it

Leave the group idle past `idleBankSeconds` (600s) so the whole set banks. Confirm
the bank in noded logs. Then re-issue a `kubectl` from a fresh session to wake it,
and check for the pod:

```python
import subprocess
print(subprocess.run(["kubectl", "get", "pod", "drill-probe", "-o", "wide"],
                     capture_output=True, text=True).stdout)
```

Record which happened:

- **Warm relight**: the pod is still there (the banked snapshot preserved sqlite +
  the running pod). The warm relight path worked.
- **Fresh boot**: the pod is gone (a partial/unreadable warm set fresh-booted, or
  the bundle TTL/roll fired). This is the WARMTH-ONLY contract holding by design,
  not a failure. Record it as such.

## Latency table (POST-MERGE, do NOT fabricate)

| Metric                                                    | Value (drill) |
| --------------------------------------------------------- | ------------- |
| fresh boot: create-to-all-Ready (all 3 members)           | TODO          |
| warm relight: connect-to-kubectl-response (banked group)  | TODO          |
| clock delta observed per member (server)                  | TODO          |
| clock delta observed per member (agent-0)                 | TODO          |
| clock delta observed per member (agent-1)                 | TODO          |
| memory floor (server, from the Task 3 spike)              | TODO (spike)  |

## What the PR description must contain (acceptance)

- the `kubectl get nodes` output showing three Ready nodes (Step 1).
- pod deployed and Running (Step 2).
- the post-bank wake result: pod preserved (warm relight) or gone (fresh boot,
  warmth-only contract held) (Step 3).
- the latency table filled from the drill (fresh boot, warm relight, per-member
  clock deltas).
