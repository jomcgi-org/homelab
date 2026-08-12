# EmberVM

Self-hosted Firecracker orchestration: **a private Lambda equivalent on
metal you own**. An organization sizes (or elastically bounds) a
Firecracker nodepool; EmberVM provides placement, fairness, isolation,
metering, and lifecycle so internal workloads get Lambda-shaped submit /
scale-to-zero / warm-serve behaviour without a hosted FaaS product,
without a Kubernetes object per invocation, and without etcd in the
execution path.

Five workload classes ride one substrate:

| Class | One line |
| ----- | -------- |
| **task** | Fresh VM per invocation, destroyed after one task; vsock only, no NIC |
| **session** | Bank/relight sandbox: idle snapshot to disk, restored on next invoke |
| **serving** | Warm HTTP endpoint; Envoy routes hits, control plane only on miss/wake |
| **stateful** | Scale-to-zero singleton datastore with a node-local authoritative volume |
| **composite** | Multi-VM group with whole-set bank/relight |

## Where everything is

| Read | For |
| ---- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The design, standalone: current state and decided future, the capability matrix, invariants, threat model, and platform contract |
| [STPA.md](STPA.md) | The safety model: unsafe control actions and feedback that drive the system into a loss, with severity |
| [deploy/README.md](deploy/README.md) | The reference deployment: fleet shape, node enrollment, warmth GC operations |
| [docs/decisions/embervm/README.md](../../docs/decisions/embervm/README.md) | Why: the ADR map and the rationale behind the architecture |
| [specs/](specs/) | TLA+ models (`adoption`, `bank_relight`, `quota`), run under TLC in the build |

## Layout

| Directory   | What it is                                                              |
| ----------- | ----------------------------------------------------------------------- |
| `control/`  | Elixir control plane: dispatcher, op-log, class managers, xDS publisher |
| `noded/`    | Go node daemon: Firecracker driver, vsock, tap/DNAT, node-local activators |
| `crd/`      | Workload CRD samples                                                   |
| `proto/`    | gRPC contract between the control plane and noded                      |
| `runtimes/` | Guest runtimes (zip lane plus bazel, claude, k3s, postgres); vsock guest contract in its README |
| `tokenbroker/`, `image/`, `scratch-prep/` | Token broker, base image build, scratch provisioning |
| `xds/`      | Envoy endpoint publisher sidecar                                       |
| `chart/`, `deploy/` | Helm chart and ArgoCD wiring                                   |
| `specs/`    | TLA+ models for adoption, bank/relight, and quota protocols            |

Everything builds in Bazel, including the Elixir control plane via a
hermetic OTP toolchain with pinned hex dependencies. Images are apko-based;
noded ships dual-arch.
