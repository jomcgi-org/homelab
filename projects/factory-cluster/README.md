# Factory home cluster

For the request path, storage layout and planned changes, see the [cluster architecture](ARCHITECTURE.md).

## Operator inputs

Before bootstrap, provide the following inputs.

1. **1Password Connect server** `factory` with read access to the `k8s-homelab`
   vault. Its credentials file and an access token are the inputs to
   `bootstrap-gitops.sh`. They are credentials for the secret broker itself, so
   they cannot come from the broker.
2. **1Password item** `k8s-homelab/factory-cloudflare-api-token`, with a field
   `CLOUDFLARE_API_TOKEN` holding a Cloudflare API token that has:
   Account: Cloudflare Tunnel:Edit; Zone `jomcgi.dev`: DNS:Edit and Zone:Read.
3. **DHCP reservations** for node-1 (192.168.4.80), node-2 (192.168.4.81) and node-3 (192.168.4.76).

## Bootstrap and rebuild

The scripts run from node-1, with SSH and passwordless sudo on the nodes.
They are idempotent. The host firewall admits only the node IPs and the
tailnet, so reach node-1 over Tailscale and run them there; another LAN
machine cannot reach the nodes.

```bash
# 1. Wipe a node's previous install (destructive; confirm with its hostname).
ssh node-2 'sudo bash -s -- --yes node-2' < projects/factory-cluster/nodes/reset-node.sh

# 2. Install or upgrade k3s and gVisor on all nodes; writes ~/.kube/factory.yaml.
projects/factory-cluster/nodes/bootstrap-nodes.sh

# 3. Hand the cluster to ArgoCD (once).
OP_CREDENTIALS_FILE=~/factory-1password-credentials.json \
OP_CONNECT_TOKEN=... \
  projects/factory-cluster/bootstrap-gitops.sh

# 4. Generate Agent Substrate's CAs, JWT keys and snapshot-store key (once).
projects/factory-cluster/bootstrap-substrate.sh
```

Nodes register after step 2 but stay NotReady: there is no CNI yet. Step 3
installs Cilium first, then creates the two 1Password bootstrap Secrets,
installs Connect and ArgoCD from this repository's charts, and applies
`factory-root-application.yaml`. From then on, merged changes under
`projects/platform-factory/` deploy automatically, and ArgoCD adopts the
Cilium, Connect and ArgoCD installs.

To upgrade k3s or gVisor, change the pins in `nodes/install-node.sh` and rerun
step 2. It restarts one node at a time.

To turn off zram swap and pod swap, remove them from `nodes/` first. Then, on
each node, delete the files that `install-node.sh` installed, because a rerun
does not remove them, and release the device:

```bash
ssh node-2 'sudo rm -f /etc/systemd/zram-generator.conf \
  /var/lib/rancher/k3s/agent/etc/kubelet.conf.d/50-swap.conf \
  /etc/systemd/system/k3s.service.d/10-gogc.conf &&
  sudo systemctl daemon-reload && sudo swapoff /dev/zram0'
```

Rerun step 2 to restart k3s without them. Without the generator configuration,
no zram swap starts at the next boot.

To change only the host firewall, copy `nodes/factory-firewall.nft` to
`/etc/factory/firewall.nft` on each node and run
`sudo systemctl reload factory-firewall`; k3s keeps running. A reload swaps
the rules in one nft transaction, so a file that fails to parse leaves the
running rules in place. Do not use `restart`: it deletes the table first. Rerunning step 2
also applies it, but restarts k3s on each server, one at a time. To turn the
firewall off on a node, run
`ssh node-2 'sudo systemctl disable --now factory-firewall'`. Stopping the unit
deletes only its own table. Add a node IP to the `nodes` set in
`nodes/factory-firewall.nft` before the node joins.

A lost cluster is rebuilt with steps 2 to 4. That recreates everything in
Git and new Substrate keys, but not Substrate's state: its PostgreSQL (actors,
templates, atespaces) and its actor snapshots have no backup yet, so they are
lost with the cluster or with the node holding their volume. The Substrate
CRDs, namespaces and snapshot volume are marked `Delete=false`, so deleting
or renaming the ArgoCD Application does not remove them.

Kubernetes Secrets are encrypted at rest (`secrets-encryption: true` in
`nodes/k3s-config.yaml`). etcd snapshots are encrypted with the cluster token
on each server (`/var/lib/rancher/k3s/server/token`); keeping a copy off the nodes is part of
the [backup plan](ARCHITECTURE.md#direction).

## Network policy

Ordering matters inside an Application. A pod that starts before its policy
exists is dropped until the policy lands; for a Job that can mean failing
outright. Envoy Gateway's certgen Job is a Helm pre-install hook, which ArgoCD
runs before any ordinary resource, so its policy is a PreSync hook as well.
Authentik's policies carry an earlier sync wave than its secret Job.

A backend behind the edge Gateway needs an egress rule on the Envoy proxy
(`projects/platform-factory/edge/chart/templates/network-policy.yaml`) and an
ingress rule from the proxy in its own namespace.

To see what a policy drops:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --verdict DROPPED -f
```

That shows one node; run it against each agent pod for the whole cluster.

## Dependency updates

Follow [Renovate operations](../platform/renovate/README.md) for the current
credential, the manual run and the required checks.

## factory.jomcgi.dev

Keep the edge OIDC Secret populated throughout a rollout and retain the permanent sentinel route while changing application routes.

To publish another hostname, change these together in one PR: the hostname
list in `projects/platform-factory/edge-tunnel/resources/route.yaml`, the
`factory-edge` provider's `redirect_uris` in
`projects/platform/authentik/factory-blueprints/factory-auth.yaml`, the app's
HTTPRoute on `edge/edge` and its namespace in `routeNamespaces`, its hostname
ownership rule in `edge-admission`, and the proxy egress and backend ingress policies. A route
that needs the access token uses the shared OIDC template in a policy in
`edge`, with a ReferenceGrant in the backend namespace. Token forwarding
stays off the gateway-wide policy.

To take the edge down, delete the `edge-tunnel` Application first, so
nothing is published while the rest goes, then `authentik`,
`cloudnative-pg` and `edge`, and `edge-admission` last. If Envoy Gateway's
controller goes before its Gateway, the `edge` GatewayClass keeps its
finalizer; clear it by name. The admission policies and bindings, and
`authentik/authentik-secrets` (which holds `AUTHENTIK_SECRET_KEY`), carry
`Delete=false`, so deleting their Applications leaves them; delete them by
name once the edge is gone and the key is no longer wanted. The namespaces
and CRDs stay behind.

### Authentik first run

Authentik starts with no admin password, and the internet cannot reach the
setup flow. Set it up through a port-forward:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n authentik port-forward svc/authentik-server 9000:80
# open http://localhost:9000/if/flow/initial-setup/ and set akadmin's email and password
```

Then sign in at https://factory.jomcgi.dev. The first sign-in enrols a
second factor; choose TOTP. A WebAuthn key enrolled over the port-forward is
bound to `localhost` and fails on factory-auth.jomcgi.dev. The admin and user
interfaces are reachable only through the port-forward.

### kubectl through Authentik

| Authentik group | Access |
| --- | --- |
| `authentik Admins` | `cluster-admin` |
| `factory-kube-viewers` | Reads workloads, events and pod logs in the namespaces that `kube-access/resources/viewers.yaml` lists. No Secrets, ConfigMaps, exec, port-forward or writes. Lists namespaces and nodes |

To give a person read-only access, add their account to
`factory-kube-viewers` in the Authentik admin interface (through the
port-forward in "Authentik first run"). Do not list members in the
repository, because it is public. To remove access, remove them from the
group. Their current token stops working at the edge and the API server
within ten minutes, when kubelogin refreshes it.

Each person's kubeconfig:

```yaml
apiVersion: v1
kind: Config
clusters:
  - name: factory
    cluster:
      server: https://factory-kube.jomcgi.dev
users:
  - name: factory-authentik
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1
        command: kubectl
        args:
          - oidc-login
          - get-token
          - --oidc-issuer-url=https://factory-auth.jomcgi.dev/application/o/factory-kubectl/
          - --oidc-client-id=factory-kubectl
          - --oidc-extra-scope=email
          - --oidc-extra-scope=profile
          - --oidc-extra-scope=offline_access
        # Required by the v1 exec API. kubelogin signs in through the
        # browser, so it needs no terminal input.
        interactiveMode: Never
contexts:
  - name: factory
    context:
      cluster: factory
      user: factory-authentik
current-context: factory
```

Install kubelogin first (`kubectl krew install oidc-login`, or
`brew install kubelogin`). The first command opens a browser at
factory-auth.jomcgi.dev. Cloudflare's certificate is publicly trusted, so the
kubeconfig carries no CA.

The API server reads `nodes/authentication-config.yaml` from
`/etc/rancher/k3s/authentication-config.yaml`, and reloads it while it runs.
Adding or removing its `kube-apiserver-arg` in `nodes/k3s-config.yaml` needs
a k3s restart: rerun step 2 of "Bootstrap and rebuild". The API server
fetches Authentik's keys from factory-auth.jomcgi.dev, through Cloudflare. A
watch or `logs -f` with no traffic for about 100 seconds ends at Cloudflare;
run it again.

### Break-glass

When Authentik or the edge is down, kubectl through factory-kube.jomcgi.dev
stops working. The client certificate in `~/.kube/factory.yaml` on node-1
does not depend on either. SSH to node-1 over Tailscale and use it there;
the host firewall refuses the apiserver to other LAN machines.

ArgoCD's local `admin` account is disabled. With the cluster kubeconfig,
use kubectl directly, or the ArgoCD CLI without its API server:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl config set-context --current --namespace argocd
KUBECONFIG=~/.kube/factory.yaml argocd app list --core
```

## Object storage

Check Garage membership and provisioning.

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n garage exec garage-0 -- /garage -c /etc/garage/garage.toml status
KUBECONFIG=~/.kube/factory.yaml kubectl -n garage logs job/garage-bootstrap
```

## Postgres

If `kagent-postgres-owner` or `kagent-postgres-readwrite` in the `kagent`
namespace is recreated, it stays empty until you sync the `kagent-postgres`
Application in ArgoCD: its PostSync Job `kagent-pg-setup` writes both
connection strings again. After either connection string changes, use ArgoCD
to restart the kagent controller Deployment. The controller reads both
Secrets at startup. Admission policy denies a direct `kubectl rollout restart`.

A CloudNativePG instance that starts before its Garage key exists caches
the missing credentials and archives nothing until it restarts, so
`kagent-pg-wait-for-key` (wave -1) holds the Cluster back until garage-init
has filled `kagent-pg-s3`, at most one 10-minute run on a fresh install.
If it gives up after 30 minutes (garage-init failing, say), the sync fails
and ArgoCD does not retry the same revision; once
`kubectl -n garage logs job/garage-bootstrap` shows the key, sync again:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n argocd patch app kagent-postgres --type merge \
  -p '{"operation":{"initiatedBy":{"username":"operator"},"sync":{"syncOptions":["CreateNamespace=true","ServerSideApply=true","RespectIgnoreDifferences=true"]}}}'
```

Each incarnation of the Cluster archives under its own `serverName`
(`kagent-pg-v1`) in the bucket. A Cluster created with `initdb` refuses to
archive into a prefix that already holds WALs ("expected empty archive"),
so if the Cluster is deleted and recreated while Garage survives, either
restore it from the old prefix (`bootstrap.recovery` with an
`externalClusters` entry pointing at `kagent-pg-v1`) or, to start empty,
bump `serverName` to the next version in `resources/cluster.yaml`. The old
prefix ages out only by hand.

Retry the Authentik sync if `authentik-pg-wait-for-key` exceeds its 30-minute deadline. Check that garage-init filled all three fields in `authentik/authentik-pg-s3`.

After rollout, confirm three healthy instances, `ContinuousArchiving=True`
and a completed base backup in Garage. Recovery also needs the existing
`authentik-secrets`, including `AUTHENTIK_SECRET_KEY` and the OIDC client
secrets. Their 1Password copies and an isolated restore drill remain on
[#7952](https://github.com/jomcgi-org/homelab/issues/7952). The same archive
incarnation rule and migration to the barman-cloud plugin before CNPG 1.31
apply to Authentik and `factory-db`.

### factory-db

Run `python3 -m pytest -q projects/platform-factory/factory-db/provisioning_test.py`
to check provisioning. The test runs the initializer against the declared Secret
placeholders and RBAC grants, checking that adding the reviewer preserves existing
passwords and connection strings. It also covers reruns, delayed Secret creation,
database ownership and ingress scope. PR CI runs the same test through
`//projects/platform-factory/factory-db:provisioning_test`. Live ArgoCD health and
Secret key presence are checked after deployment.

## Agent Substrate

To upgrade Substrate, rerun `projects/platform-factory/substrate/render-upstream.sh` and review the rendered diff. Verify the installation:

```bash
export KUBECONFIG=~/.kube/factory.yaml
kubectl -n ate-system get pods          # all Running and Ready
kubectl get sandboxconfig gvisor-default # DEFAULT v7
kubectl get clustertrustbundles | grep -e podcert.ate.dev -e egress-mitm
# kubectl-ate from the same release, then a throwaway actor:
kubectl ate create atespace substrate-smoke
kubectl create namespace substrate-smoke
kubectl label namespace substrate-smoke factory.jomcgi.dev/substrate-workers=true
kubectl apply -f <WorkerPool in namespace substrate-smoke, workerImage ghcr.io/kagent-dev/substrate/ateworker-gvisor:v1.0.1@sha256:...>
kubectl ate --preview create actor-template -f <template>   # wait for GOLDEN TAG
kubectl ate create actor smoke-1 -a substrate-smoke --template smoke
kubectl ate proxy actor smoke-1 -a substrate-smoke # then send it a request
```

To check credential injection, give the actor an egress policy
(`kubectl ate create egress-policy`) with an `http` rule for an in-cluster
echo server. Its `replaceHeaders` effect should name an
`ate-secret://k8s.io/default/<namespace>/<secret>/<key>` that the actor's
atespace is granted in `values/substrate.yaml`: a Secret labelled
`substrate.ate.dev/injectable=true` in a namespace where the provider has a
RoleBinding. The gateway's own policy doesn't allow the echo server, so the
check also needs two temporary CiliumNetworkPolicies, deleted afterwards. One
in `ate-system` allows `app: atenet-egress` egress to the echo pod's port, and
one in the echo server's namespace allows ingress from it. The echo server
should receive the Secret's value, and the actor should hold only its
placeholder.

Check Hubble for drops while it runs (see Network policy). Then delete the
actor, the template, the atespace, the WorkerPool and the namespace.

## Observability

Useful starting points in the `metrics` dataset, all filtered on
`k8s.cluster.name = factory`:

- Node headroom: `MAX(k8s.node.memory.working_set)`, `MAX(k8s.node.cpu.usage)`
  and `MAX(system.memory.usage)` grouped by `k8s.node.name`.
- Busiest pods: `MAX(k8s.pod.memory.working_set)` grouped by
  `k8s.namespace.name` and `k8s.pod.name`.
- Policy drops: `SUM(hubble_drop_total)` grouped by `reason`,
  `source_namespace` and `traffic_direction`.
- Scrape health: `MIN(up)` grouped by `service.name` and
  `service.instance.id`; a target at 0 is unreachable.
- etcd: `MIN(kubernetes_healthcheck)` grouped by `name` and
  `service.instance.id` (etcd and etcd-readiness, per API server), and
  `MAX(apiserver_storage_size_bytes)`.

For logs, run an environment-wide query filtered on
`k8s.cluster.name = factory` and group by `service.name` and
`k8s.namespace.name`. etcd's own metrics listen on 127.0.0.1:2381 on each node,
out of a pod's reach, so etcd health comes from the API servers' readiness
checks instead.

## Metrics view

`resources/prometheus.yml` names every target, and a new one needs three
changes: a job there, an egress rule in `resources/network-policy.yaml`, and
an ingress rule at the target in `resources/scrape-ingress.yaml`. Those
ingress rules are CiliumClusterwideNetworkPolicies so they sync whether or
not the target's namespace exists yet, and leave with Prometheus.

Edit a dashboard by changing its JSON; Grafana rejects saves from the UI. Each
ConfigMap's name carries its content hash, so a change rolls the pod.

After the OIDC client secret in `grafana/grafana-oidc` changes, restart the
Grafana Deployment to load the new value. Grafana reads it at startup.

The break-glass admin's password is generated at each start into memory:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n grafana exec deploy/grafana -c grafana -- \
  cat /run/grafana/admin-password
KUBECONFIG=~/.kube/factory.yaml kubectl -n grafana port-forward svc/grafana 3000:3000
# open http://localhost:3000/login?disableAutoLogin=true and sign in as admin
KUBECONFIG=~/.kube/factory.yaml kubectl -n prometheus port-forward svc/prometheus 9090:9090
```

### Alerts

Prometheus evaluates `resources/rules.yml` and sends firing alerts to one
Alertmanager (`resources/alertmanager.yaml`), which posts them to the homelab
Discord channel through a webhook. The path uses nothing in the hub, so an
alert arrives while the hub is down. Alertmanager keeps silences in an
emptyDir: a restart forgets them.

The webhook URL comes from 1Password item `factory-alerts-discord` in vault
`k8s-homelab`, field `webhook-url`. To create it, in the Discord channel open
Edit Channel, Integrations, Webhooks, New Webhook, and copy the URL. If the
item is missing, Alertmanager starts but logs a failed send for each alert.
To test the path:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n prometheus port-forward svc/alertmanager 9093:9093 &
curl -s -X POST -H 'Content-Type: application/json' http://localhost:9093/api/v2/alerts \
  -d '[{"labels":{"alertname":"AlertPathTest"},"annotations":{"summary":"test"}}]'
```

**FactoryCodexQuotaLow, FactoryCodexQuotaExhausted.** Read the grant's usage
on the broker's metrics: `factory_broker_grant_quota_used_percent` and
`factory_broker_quota_resets_at_seconds` (Prometheus, port-forward
`svc/prometheus` 9090). A person resets the plan or logs the broker in to
another account (`projects/factory/broker`, device login). Coder turns that
hung on HTTP 429 may need `connector cancel` afterwards.

**FactoryCodexGrantNotReady.** Log the broker in again with its device login
(`projects/factory/broker`). `refresh_blocked` means a refresh token was
reused; see the broker's logs first.

**FactoryCodexQuotaReadingStale.** Check the broker's usage reads
(`factory_broker_codex_usage_refresh_total` by outcome) and its logs.

**NodeDiskFilling.** Find the largest directories with
`ssh node-N 'sudo du -xh --max-depth=3 / | sort -h | tail -20'`. The usual
users are local-path volumes under `/var/lib/rancher/k3s/storage` (rustfs
snapshots, Garage, the databases), images (`sudo k3s crictl rmi --prune`) and,
on node-1, Claude scratch under `/tmp/claude-1000`. Ask before you delete a
directory that a running session uses.

**KagentSuspendTimedOut.** kagent v1.0.1 keeps the Actor SUSPENDING, refuses
every turn and delete for the session, and holds its worker slot. To free
every locked session:

```bash
# 1. The locked sessions: the same quiescence claim (session and executor)
#    seen twice, a minute apart. A suspend that is still running has at most
#    30 s, and the table records no claim time.
claims() { KUBECONFIG=~/.kube/factory.yaml kubectl -n kagent-pg exec kagent-pg-1 -c postgres -- \
  psql -d kagent -At -c "select distinct s.id || ' ' || e.quiescence_executor_id
    from kagent.session s join kagent.session_task_event e using (history_id)
    where e.quiescence_pending and e.quiescence_executor_id is not null" | sort; }
FIRST=$(claims); sleep 60
SESSIONS=$(comm -12 <(echo "$FIRST") <(claims) | cut -d' ' -f1 | sort -u)
KUBECONFIG=~/.kube/factory.yaml kubectl -n kagent port-forward svc/kagent-controller 18083:8083 &
for SESSION in $SESSIONS; do
  # 2. Its worker pod: homelab-codex-* for a coder, homelab-reviewer-* for a reviewer.
  POD=$(KUBECONFIG=~/.kube/factory.yaml kubectl -n ate-system exec postgres-0 -c postgres -- \
    psql -U postgres -d atepg -At -c "select substring(encode(proto, 'escape')
      from 'homelab-[a-z]+-[0-9a-f]{8,10}-[a-z0-9]{5}') from substrate.actors where name = 'session-$SESSION'")
  # The worker namespace and the session's creator, who reverts it.
  case "$POD" in
    homelab-codex-*) NS=factory-r-homelab SA=factory-connector ;;
    homelab-reviewer-*) NS=factory-r-homelab-review SA=factory-reviewer ;;
    *) echo "$SESSION: no worker pod found"; continue ;;
  esac
  # 3. Delete that pod and wait; the Actor crashes within seconds.
  KUBECONFIG=~/.kube/factory.yaml kubectl -n "$NS" delete pod "$POD" --wait=true
  sleep 10
  # 4. Revert the session as its creator. grpc-status 0 means done.
  TOKEN=$(KUBECONFIG=~/.kube/factory.yaml kubectl -n factory create token "$SA" \
    --audience kagent-invocation --duration 10m)
  echo "$SESSION:"
  python3 -c 'import struct,sys; s=sys.argv[1].encode(); m=b"\n"+bytes([len(s)])+s
sys.stdout.buffer.write(b"\0"+struct.pack(">I",len(m))+m)' "$SESSION" |
    curl -sS --http2-prior-knowledge -H 'Content-Type: application/grpc' -H 'TE: trailers' \
      -H "Authorization: Bearer $TOKEN" --data-binary @- -o /dev/null -D - \
      http://127.0.0.1:18083/kagent.api.v1.SessionService/RevertSession | grep -i grpc-status
done
kill %1
```

Run it again until step 1 finds no session. Revert before the connector
sends to the session again. A connector recovery hold left by a send to the
crashed session needs `connector recovery confirm-fenced`.

## itsaplan

itsaplan (`projects/factory/ARCHITECTURE.md`, "itsaplan") serves
factory-plan.jomcgi.dev. Its break-glass admin signs in there with email
and password after the edge's Authentik sign-in, for when itsaplan's own OIDC
client fails; invite people from it before their first OIDC sign-in, since
registration is invite-only. The browser calls the api on the public origin,
so the UI does not work over a port-forward; the api does, for scripts.

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory get secret factory-itsaplan-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

If those keys are lost while the database has accounts, settings or stored
secrets, `factory-itsaplan-secret-init` fails the sync with `REFUSING to
generate` rather than replace them. It counts the rows read-only with the api
image's psql before it writes anything. Recover in this order:

1. While the api still runs, its pods hold the keys in their environment.
   Copy them back into the Secret and sync again:

   ```bash
   KUBECONFIG=~/.kube/factory.yaml kubectl -n factory exec deploy/factory-itsaplan-api -c api -- \
     printenv BETTER_AUTH_SECRET APP_ENCRYPTION_KEY
   ```

2. Otherwise restore the old values from wherever a copy survives (1Password
   once it holds them, or an etcd snapshot), and sync again.
3. With no copy left, accept the loss and sync again:

   ```bash
   KUBECONFIG=~/.kube/factory.yaml kubectl -n factory annotate secret factory-itsaplan \
     factory.jomcgi.dev/regenerate-itsaplan-keys=true
   ```

   secret-init then deletes every `app_secret` row (the OIDC, Google, mail,
   SCIM and bot settings, all encrypted with the lost key; itsaplan decrypts
   the stored OIDC settings before it accepts new ones, so they cannot simply
   be overwritten), generates new keys, removes the annotation and restarts
   the api and worker, whose running pods would otherwise keep using the old
   keys. The keys' generation ID is stored on the Secret with them and set on
   both pod templates on every run, so a restart interrupted by a failure
   completes when secret-init runs again: its container retries, and
   otherwise the Job reruns at the first chart upgrade. The bootstrap then writes the OIDC settings afresh through the new
   pods. Every session ends, so people sign in again. Anything else encrypted
   with the old key stays unreadable until it is entered again in the app:
   mail, Google, SCIM and bot settings, workspace SCIM tokens, Git
   connections, integration credentials and agent invocation secrets. An
   annotation secret-init finds when no keys are missing is removed unused.

## Codex login

The broker holds the factory's one Codex grant
(`factory/factory-broker-grant-codex-primary`), filled by its own device
login. Operators call its admin port as `factory/factory-operator`, which no
Pod may run as:

```bash
export KUBECONFIG=~/.kube/factory.yaml
kubectl -n factory port-forward deploy/factory-broker 8081:8081 &
sleep 2
TOKEN=$(kubectl -n factory create token factory-operator --audience factory-broker --duration 10m)
# Prints verification_url and user_code: open the URL, sign in to the
# ChatGPT account the factory uses, and enter the code.
curl -s -X POST -H "Authorization: Bearer $TOKEN" localhost:8081/admin/codex/login
# login "granted" and grant_state "present" once approved.
curl -s -H "Authorization: Bearer $TOKEN" localhost:8081/admin/codex/login
# Force a refresh, e.g. to check the grant after an account change.
curl -s -X POST -H "Authorization: Bearer $TOKEN" localhost:8081/admin/codex/refresh
kill %1
```

The broker waits up to 16 minutes for approval, longer than the token lives:
mint a new token if a status call returns 401. The login state is held in
memory, so a broker restart resets it to `none`; a stored grant survives in
its Secret. The ChatGPT account needs device-code sign-in for Codex enabled
in its security settings.

## Connector

Keep kagent’s ServiceAccount authority stable across connector upgrades so the connector retains access to its sessions.

Operators stop delivery for an issue, or clear a credential or quota block
after fixing the grant, from the connector's own pod:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory exec deploy/factory-connector -- \
  connector cancel HOME-7 --by "$USER"
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory exec deploy/factory-connector -- \
  connector unblock kagent:factory-r-homelab/homelab-codex
```

## Reviewer

Include the binding namespace in kagent RBAC and in Substrate’s credential provider policies. Render and sync a changed Substrate policy before enabling the reviewer. Install App `jomcgi-factory-review` (5758326) on the bound
repository and populate `k8s-homelab/factory-review-gh-app` with `app-id`,
`installation-id` and `private-key`. Its PR comment
(`reviewer.commentAuthor`) needs the App's Issues read and write permission,
accepted on the installation; without it, comment writes fail and the check
is unaffected. The broker alone mounts those keys.
Set `reviewer.bindings.homelab.requiredChecks` to the required CI identities,
currently `[{name: pr-checks, statusCreator: "buildbuddy-io[bot]"}]`.
The default `enqueueAuthors: [jomcgi]` lets the reviewer enqueue Joe's
passing PRs; the connector still enqueues the factory's PRs.
The enable PR needs Joe's approval of its exact head. The connector's
`reviewerCheck` switch and the ruleset requiring `factory review` are
separate changes.

On a PR's current head, open Checks and select `factory review` from the
reviewer App. It starts in progress and completes with findings or success;
a human gate waits for Joe's approval on that head. The reviewer's comment
on the PR's conversation shows the same verdict and findings, updated in
place. Read service logs and
metrics with:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory logs deploy/factory-reviewer --tail=100
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory port-forward deploy/factory-reviewer 9090:9090
# In another terminal:
curl -fsS localhost:9090/metrics
```

`factory_reviewer_reviews_in_flight`,
`factory_reviewer_reviews_completed_total`,
`factory_reviewer_human_gates_total` and
`factory_reviewer_enqueues_total` show progress; retries and GitHub/kagent
error counters explain stalled checks. Metrics have no scraper ingress yet.

Disable through a deploy-values PR setting `reviewer.enabled: false`.
ArgoCD removes the service and its network grants; the ServiceAccount and
review namespaces remain kept to preserve identity and state. Existing
checks stay on GitHub, and disabling does not cancel a running kagent task.
If a later ruleset requires the check, new heads stay blocked until review
is restored. Preserve that gate. Never scale the Deployment above one
replica: two instances review every head twice.
