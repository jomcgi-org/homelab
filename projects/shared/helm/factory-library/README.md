# Factory Helm library

`factory-library` is a library chart for the factory cluster. It renders
Deployment, CronJob, Job, Service, CiliumNetworkPolicy, ServiceAccount and secret
resources. Bucket entries add Garage keys and optional R2 mirrors. It does not
render routes or namespaces. The first consumer is `projects/factory-landing/chart` (Refs HOME-69).

## Wiring a chart

Add this dependency to a service chart's `Chart.yaml`:

```yaml
dependencies:
  - name: factory-library
    version: "0.1.0"
    repository: "file://../../shared/helm/factory-library/chart"
```

Adjust the relative path for deeper charts. Put this in `templates/workloads.yaml`:

```gotemplate
{{ include "factory-library.all" . }}
```

Run `helm dependency update <consumer-chart>` and commit `Chart.lock` and
`charts/factory-library-*.tgz`. Bazel's `helm_chart` packages that archive;
`ci regen` checks and refreshes its contents against the library source. The
library version and each consumer version remain under CI's versioning rules.

Keep `helm_chart(images = {"image": "//projects/<service>:image.info"})` for a
single image, or map `workloads.<name>.image` for multiple images. Packaging
injects the repository and digest. Source renders must supply a digest explicitly.
The landing chart shows how its lint and overlay tests consume the generated
image values without a tag fallback or a committed production digest.

The named templates `factory-library.deployment`, `factory-library.cronjob`,
`factory-library.job` and `factory-library.service` accept
`(dict "root" . "name" $name "workload" $values)`. Each workload template also
renders its policy, optional Service and optional ServiceAccount. Use `all` for
ordinary charts; it validates the entire exception list and also renders secrets.
Workload names are stable Kubernetes DNS labels, not release-prefixed names.
Do not reuse them in another release in the same namespace.

## Values reference

```yaml
image:
  repository: ghcr.io/jomcgi/homelab/projects/example
  digest: "" # Supplied by helm_chart(images), or explicitly for a local render.

workloads:
  example:
    kind: Deployment
    replicas: 2
    containerName: web
    ports:
      - name: http
        containerPort: 8080
    resources:
      requests: { cpu: 10m, memory: 32Mi }
      limits: { memory: 128Mi }
    writablePaths:
      - name: tmp
        mountPath: /tmp
        sizeLimit: 16Mi
    readinessProbe:
      httpGet: { path: /, port: http }
    service:
      ports:
        - { name: http, port: 8080, targetPort: http }
    network:
      ingress:
        - { peer: edge, port: 8080 }
      egress:
        - { peer: garage, port: 3900 }
        - { peer: github-api, fqdn: api.github.com, port: 443 }

secrets: {}
exceptions: []
```

| Value | Behaviour |
| --- | --- |
| `image` | Optional shared image; each workload can instead set `image`. A repository without a tag and a `sha256:` digest with 64 lowercase hex digits are required. An injected `tag` is ignored. |
| `workloads.<name>.kind` | `Deployment` (default), `CronJob` or `Job`. |
| `containerName` | Defaults to the workload name. One container per workload. |
| `replicas` | Deployment replicas, default 1; zero is preserved. |
| `schedule`, `concurrencyPolicy` | CronJob schedule is required. Concurrency defaults to `Forbid`; `Allow` and `Replace` are also accepted. |
| `backoffLimit` | Job and CronJob retry count, default 3; zero is preserved. |
| `hook` | Jobs only: `Sync` or `PostSync`. Renders the Job as an Argo CD hook with `BeforeHookCreation,HookSucceeded` deletion, so it runs on every sync. Use it for a Job whose spec changes each release, such as a deploy or a post-deploy check; a plain Job's spec is immutable. |
| `resources` | Kubernetes resource map. CPU and memory requests and a memory limit are mandatory. |
| `command`, `args`, `env`, `ports` | Standard Kubernetes container fields. Secret environment variables use `valueFrom.secretKeyRef`. |
| `readinessProbe`, `livenessProbe`, `startupProbe` | Standard Kubernetes probes; no probe is invented. |
| `writablePaths` | List of `name`, `mountPath`, optional `sizeLimit` and `medium`. Each becomes an `emptyDir` and a mount. No host paths, arbitrary volumes or extra containers. |
| `kubernetesApi` | Boolean, default false. True sets pod token automount and creates a dedicated ServiceAccount with the workload's name. It grants no RBAC. Declare API egress separately. |
| `service.ports` | Standard Service ports; supplying `service` renders a ClusterIP Service selecting this workload. |
| `network.ingress`, `network.egress` | Lists of `{peer, port, protocol?}`. Protocol defaults to TCP; UDP and SCTP are accepted. Every rule requires a numeric port from 1 to 65535. |
| `secrets` | Named references or generated secrets, described below. |
| `exceptions` | The single list of reviewed departures from defaults. |

Every pod runs as non-root UID/GID 65532 with `RuntimeDefault` seccomp. Every
container drops all capabilities, disallows privilege escalation and has a
read-only root filesystem. Jobs restart with `Never`. Images under
`ghcr.io/jomcgi/` automatically name `ghcr-imagepull-secret`; provision that
Secret in the same namespace, normally through the OnePasswordItem below.

Raw pod, container security, volume and network selector overrides are rejected.
Image digests, required resources, non-root execution, capability dropping,
seccomp and privilege escalation protection cannot be waived by this API.

## Peer catalogue

Each workload gets a policy with `enableDefaultDeny.ingress` and `.egress` true.
An empty list allows no traffic in that direction. Cilium allow policies are
additive: the factory's cluster-wide baseline still allows DNS to CoreDNS;
the library does not expand that allowance. An ingress declaration grants only
the receiving side; a remote peer may also need an egress rule, and vice versa.

The selectors below are fixed in `chart/templates/_peers.tpl`, copied from the
factory and platform-factory policies. Consumers cannot replace their labels.
`namespace` means `k8s:io.kubernetes.pod.namespace`; name, instance and component
mean the corresponding `app.kubernetes.io/*` labels.

| Peer | Namespace | Other selector labels |
| --- | --- | --- |
| `edge` | `edge` | `gateway.envoyproxy.io/owning-gateway-name=edge`, `gateway.envoyproxy.io/owning-gateway-namespace=edge` |
| `garage` | `garage` | name=`garage` |
| `itsaplan-api` | `factory` | name=`factory-itsaplan`, instance=`factory`, component=`api` |
| `itsaplan-web` | `factory` | name=`factory-itsaplan`, instance=`factory`, component=`web` |
| `itsaplan-worker` | `factory` | name=`factory-itsaplan`, instance=`factory`, component=`worker` |
| `kagent-controller` | `kagent` | name=`kagent`, instance=`factory`, component=`controller` |
| `kagent-ui` | `kagent` | name=`kagent`, instance=`factory`, component=`ui` |
| `broker` | `factory` | name=`factory-broker`, instance=`factory` |
| `connector` | `factory` | name=`factory-connector` |
| `reviewer` | `factory` | name=`factory-reviewer`, instance=`factory` |
| `factory-db` | `factory-db` | `cnpg.io/cluster=factory-db` |
| `kagent-postgres` | `kagent-pg` | `cnpg.io/cluster=kagent-pg` |
| `substrate-api` | `ate-system` | `app=ate-api-server` |
| `substrate-idp` | `ate-system` | `app=ate-idp-server` |
| `substrate-router` | `ate-system` | `app=atenet-router` |
| `substrate-egress` | `ate-system` | `app=atenet-egress` |
| `kube-apiserver` | Cilium entity | Egress only, requires `kubernetesApi: true`; use backend port 6443 on the factory's k3s cluster. |

An external peer has its own explicit name, exact `fqdn` and port, for example
`{peer: github-api, fqdn: api.github.com, port: 443}`. FQDN peers are egress only
and cannot reuse a catalogue or broad peer name. Unknown names without an FQDN
fail. `world`, `0.0.0.0/0`, `::/0` and wildcard FQDNs require exact exceptions.
Add a new internal peer centrally, with a test and the current policy's labels.

## Secrets

```yaml
secrets:
  ghcr-imagepull-secret:
    workload: example
    onePassword:
      itemPath: vaults/k8s-homelab/items/ghcr-read-permissions
      type: kubernetes.io/dockerconfigjson
  example-token:
    workload: example
    generated:
      keys:
        token: 32 # Random entropy bytes, 16–128 per key; never a literal value.
      image:
        repository: ghcr.io/jomcgi/homelab/projects/python-runtime
        digest: "" # Supply the digest of an image providing Python 3 + CA roots.
      resources:
        requests: { cpu: 10m, memory: 32Mi }
        limits: { memory: 128Mi }
```

The Python runtime repository above is illustrative; supply an actual built
image. Map `secrets.<name>.generated.image` in `helm_chart(images)` if using a
repository-built runtime.

`onePassword` accepts only `itemPath` (`vaults/<vault>/items/<item>`) and optional
Secret `type` (default `Opaque`). It renders a OnePasswordItem in the release
namespace at sync wave -2.

`generated` accepts only key entropy sizes, image and resources. It renders an
empty Opaque Secret with `Prune=false,Delete=false` and
`helm.sh/resource-policy: keep`, and a restricted Python initialization Job. No
secret material appears in values or offline renders. There is no `data`,
`stringData`, literal-value, template-evaluation or Helm random-value interface.

The Job runs as an ArgoCD Sync hook at wave -1, after its wave -2 Secret,
ServiceAccount, Role, RoleBinding and Cilium policy. It can only get and patch
that named Secret and reach the API server on port 6443. It fills missing keys
using Python's cryptographic random generator. A JSON Patch resource-version
test makes concurrent initializers retry without overwriting the winner; existing
keys are never replaced. A later sync recreates the hook, rechecks the Secret
and exits without rotation. Deleting a key deliberately allows it to regenerate;
removing a key from values does not erase it from the kept Secret.

For generated Secrets, the consuming ArgoCD Application must ignore `/data` on
the named Secret, with `RespectIgnoreDifferences=true` and server-side apply.
This avoids treating the runtime-owned data as Git drift. The library cannot
configure its parent Application. Keep these settings and keep annotations
when changing or retiring a service. Plain `helm template` verifies manifests;
the sync hook lifecycle is designed for ArgoCD.

## Buckets and public mirrors

A chart declares its buckets once. Every workload in that chart gets Garage
egress on port 3900. Secret initializer Jobs keep only their API egress.

```yaml
buckets:
  - name: factory-public
    access: write # read, write or readwrite; never owner
    public:
      r2Bucket: factory-public
      itemPath: vaults/k8s-homelab/items/factory-r2-public-mirror
      # schedule: "* * * * *"
```

Each entry creates a kept Secret named RELEASE-BUCKET-s3. Its fields are
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL, AWS_REGION and BUCKET.
Consumers reference those fields through their workload env declarations.
The ArgoCD Application must ignore /data for each claim Secret and set
RespectIgnoreDifferences=true and ServerSideApply=true.

Garage provisioning uses the HOME-137 fallback: garage-init discovers Secrets
labeled factory.jomcgi.dev/garage-bucket and factory.jomcgi.dev/garage-access.
It runs after Garage syncs and every ten minutes. No credentials are generated
during Helm rendering. Existing keys survive retries and Garage rebuilds.
A narrower access value revokes unwanted permissions without rotating the key.

Discovery lists namespace metadata, then labeled Secret metadata in namespaces
that grant list access. Each consumer renders that namespaced list Role and
get/update Roles restricted to its claim names. Kubernetes RBAC cannot restrict
list by label or metadata. The trusted garage-init account can therefore read
all Secrets in opted-in namespaces, including the mirror credential.
It receives no cluster-wide Secret permission. Retiring a kept claim requires
an explicit Garage key revocation; deleting the chart does not revoke the key.

Adding public creates a separate read-only Garage key, a OnePasswordItem and
a mirror CronJob. The Job runs every minute with Forbid concurrency, its own
ServiceAccount and no mounted token. It uses the stock rclone/rclone image at
the 1.76.0 multi-platform digest. The optional public.image accepts only the
stock repository and a SHA-256 digest; tags fail rendering.

The digest comes from the
[upstream 1.76.0 package](https://github.com/rclone/rclone/pkgs/container/rclone/1515701921?tag=1.76.0).
The [upstream image workflow](https://github.com/rclone/rclone/blob/v1.76.0/.github/workflows/build_publish_docker_image.yml)
publishes the same manifest to Docker Hub and GHCR.

```mermaid
flowchart LR
  claim[Labeled Garage Secret] -->|reconcile| init[garage-init]
  init -->|bucket and scoped key| garage[Garage]
  item[1Password item] -->|operator| credential[R2 Secret]
  garage -->|read on 3900| mirror[rclone CronJob]
  credential -->|environment| mirror
  mirror -->|sync payloads, then manifest on 443| r2[R2 bucket]
```

The item supplies access-key-id, secret-access-key and s3-api-endpoint.
Only RCLONE_CONFIG_* environment variables configure remotes; no rclone config
file or credential bytes enter the chart. Garage uses Other with path-style
addressing; R2 uses Cloudflare with region auto and path-style addressing.

The policy allows Garage:3900, DNS to CoreDNS, and the existing account's exact
R2 hostname on 443. The script rejects an endpoint that differs from that host.
A new R2 account requires a reviewed change to factory-library.r2Host.
No wildcard R2 egress is rendered.

A kept ValidatingAdmissionPolicy and Deny binding restrict the R2 Secret to
that mirror's ServiceAccount in its namespace. They check direct and projected
volumes, env, envFrom, imagePullSecrets, init containers and ephemeral containers.
These policies govern Pod references, not direct Kubernetes Secret API reads.

Each run syncs payloads excluding the root manifest.json with
Cache-Control: public, max-age=31536000, immutable. After a successful sync,
it copies an existing manifest with Cache-Control: public, max-age=60.
A failed sync or listing stops publication. An absent source manifest removes
the destination manifest. Payload deletions use rclone sync's normal deletion.

The publisher must write immutable objects before replacing its manifest.
Keep objects referenced by a current or cached manifest until those readers
expire; the mirror does not provide a transaction across concurrent writes.
Only one chart should own public for a destination. The later publisher declares
its own write bucket entry without public and cannot mount the R2 Secret.

See [factory-public](../../../factory-public/README.md) for the first deployment,
the operator trial result, and live acceptance.

## Exceptions

Each entry requires an existing workload name, an exact default name and a
nonblank reason. Duplicate entries, unknown defaults and unknown workloads fail.

```yaml
exceptions:
  - workload: example
    default: runAsUser
    value: 1000
    reason: The upstream image owns its application files as UID 1000.
  - workload: example
    default: network.fqdn:*.example.com
    reason: The upstream API assigns a hostname to each tenant.
```

| Default name | Effect |
| --- | --- |
| `runAsUser`, `runAsGroup` | Set `value` to a positive numeric UID/GID; root remains prohibited. |
| `readOnlyRootFilesystem` | Sets this field to false. Prefer a declared `emptyDir` when possible. |
| `network.world`, `network.0.0.0.0/0`, `network.::/0` | Allows that exact peer, still with an explicit port. |
| `network.fqdn:<pattern>` | Allows only that exact wildcard FQDN pattern, still named and port-scoped. |
| `network.defaultDeny` | Omits `enableDefaultDeny`, relying on the factory's kept cluster-wide baseline. Reserved for migrations that must preserve an existing policy's spec. It does not create an allow rule. |

Only UID/GID exceptions accept `value`. Exceptions do not grant network access
without a corresponding peer declaration and do not affect other workloads.
The complete list for a workload appears as JSON in
`factory.jomcgi.dev/exceptions` on its objects and pod template. This makes each
reason visible in the rendered manifest.

Factory-landing uses `network.defaultDeny` to retain its existing policy exactly,
including the absence of `egress`. Its current cluster baseline supplies deny
in both directions and DNS. No pod security exception is needed.

## Validation

```bash
ci lint
ci regen
ci ratchet
HELM_BIN="$(command -v helm)" python3 -m pytest \
  projects/shared/helm/factory-library/library_test.py \
  projects/factory-landing/chart/landing_test.py
```

Golden objects cover all three workload types, mandatory hardening, public and
private images, ServiceAccounts, writable paths, services, peers, exceptions and
both secret modes. Refusal goldens pin error messages. The initializer tests
exercise preservation and optimistic-concurrency conflicts without a cluster.
The landing test retains its original assertions and compares the whole render
to `chart/testdata/before.json`, ignoring only annotations.

For the PR body, render both revisions with the same test digest and print their
canonical diff (resource order and YAML formatting are normalized):

```bash
HELM_BIN="$(command -v helm)" python3 \
  projects/shared/helm/factory-library/render_landing_diff.py
```
