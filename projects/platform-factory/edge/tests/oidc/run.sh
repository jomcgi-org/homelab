#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
chart=../../chart
[[ $(kubectl config current-context) == kind-edge-oidc ]]
projects=../../../..
namespaces=(edge kagent t3 factory-landing argocd factory grafana garage factory-discord)
for ns in "${namespaces[@]}"; do kubectl create namespace "$ns"; done

# Use the CRDs and controller shipped with the edge chart.
kubectl apply -f "$projects/platform-factory/priority/priority-class.yaml"
helm template crds "$chart"/charts/gateway-crds-helm-*.tgz --set crds.gatewayAPI.enabled=true --set crds.envoyGateway.enabled=true | kubectl apply --server-side -f -
# Exercise the production RoleBindings against the API server's authorizer.
kubectl apply -f "$projects/platform-factory/kube-access/resources/sessions.yaml"
bash "$projects/platform-factory/kube-access/check-session-access.sh" \
	--as=oidc-test-session --as-group=authentik:factory-kube-sessions

helm install edge "$chart"/charts/gateway-helm-*.tgz -n edge --skip-crds --wait \
	--set crds.enabled=false --set deployment.priorityClassName=factory-platform \
	--set config.envoyGateway.provider.type=Kubernetes \
	--set config.envoyGateway.provider.kubernetes.deploy.type=GatewayNamespace

tmp=$(mktemp -d)
cleanup() {
	local result=$?
	if ((result != 0)); then
		kubectl get gateways,httproutes,securitypolicies,pods -A -o wide || true
		if [[ -f "$tmp/forward.log" ]]; then cat "$tmp/forward.log"; fi
	fi
	rm -rf "$tmp"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=issuer.edge.svc.cluster.local' \
	-addext 'subjectAltName=DNS:issuer.edge.svc.cluster.local' -keyout "$tmp/key.pem" -out "$tmp/ca.pem" 2>/dev/null
kubectl -n edge create configmap issuer-ca --from-file="$tmp/ca.pem"
for ns in "${namespaces[@]}"; do
	kubectl -n "$ns" create secret generic fixture --from-file=issuer.py --from-file="$tmp/key.pem" --from-file="$tmp/ca.pem"
	kubectl -n "$ns" apply -f fixture.yaml
	kubectl -n "$ns" rollout status deploy/fixture
done
kubectl -n edge expose deployment fixture --name issuer --port 443 --target-port 8443
kubectl -n kagent expose deployment fixture --name kagent-ui --port 8080
kubectl -n t3 expose deployment fixture --name t3-relay --port 8080
kubectl -n t3 apply -f "$projects/platform-factory/t3/resources/reference-grant.yaml"
# Garage's website endpoint for artifact.jomcgi.dev; the echo backend
# answers every path.
kubectl -n garage expose deployment fixture --name garage-web --port 3902 --target-port 8080
kubectl -n garage apply -f "$projects/platform-factory/garage/resources/reference-grant.yaml"
# A real one-node Garage S3 API for signed share links (route
# artifact-share): a read-write key uploads, a read-only key signs.
kubectl -n garage create configmap garage-config --from-file=garage.toml
kubectl -n garage apply -f garage.yaml
kubectl -n garage rollout status deploy/garage --timeout=180s
garage() { kubectl -n garage exec deploy/garage -- /garage -c /etc/garage/garage.toml "$@"; }
node=$(garage node id -q | cut -d@ -f1)
garage layout assign -z kind -c 1G "$node"
garage layout apply --version 1
garage bucket create bosun-artifacts
export SHARE_RW_ID=GK000000000000000000000001 SHARE_RW_SECRET=$(openssl rand -hex 32)
export SHARE_RO_ID=GK000000000000000000000002 SHARE_RO_SECRET=$(openssl rand -hex 32)
garage key import --yes -n rw "$SHARE_RW_ID" "$SHARE_RW_SECRET"
garage key import --yes -n share "$SHARE_RO_ID" "$SHARE_RO_SECRET"
garage bucket allow --read --write bosun-artifacts --key rw
garage bucket allow --read bosun-artifacts --key share
# The notes viewer for notes.jomcgi.dev, with the grant the factory chart
# renders (templates/discord-notes-view.yaml).
kubectl -n factory-discord expose deployment fixture --name bosun-notes-view --port 8080
kubectl -n factory-discord apply -f - <<'YAML'
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: edge-bosun-notes-view
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: edge
  to:
    - group: ""
      kind: Service
      name: bosun-notes-view
YAML

# Use the production HTTPRoutes with fixture services at their backend ports.
kubectl -n factory-landing expose deployment fixture --name factory-landing --port 8080
kubectl -n argocd expose deployment fixture --name argocd-server --port 80 --target-port 8080
kubectl -n factory expose deployment fixture --name factory-itsaplan-web --port 3001 --target-port 8080
kubectl -n factory expose deployment fixture --name factory-itsaplan-api --port 3000 --target-port 8080
kubectl -n grafana expose deployment fixture --name grafana --port 3000 --target-port 8080
kubectl -n grafana expose deployment fixture --name runbooks --port 8080
# Only the route is installed; satisfy the landing chart's packaging-time image check.
helm template factory-landing "$projects/factory-landing/chart" -n factory-landing --set-string "image.digest=sha256:$(printf '%064d' 0)" -s templates/httproute.yaml | kubectl -n factory-landing apply -f -
helm template argocd "$projects/platform/argocd" -n argocd -f "$projects/platform/argocd/values-factory.yaml" -s charts/argo-cd/templates/argocd-server/httproute.yaml | kubectl apply -f -
helm template factory "$projects/factory/chart" -n factory -f "$projects/factory/chart/values-homelab.yaml" -s templates/itsaplan-route.yaml | kubectl apply -f -
kubectl apply -f "$projects/platform-factory/grafana/resources/route.yaml"
kubectl apply -f "$projects/platform-factory/grafana/resources/runbooks-route.yaml"

templates=()
for template in gateway security-policy kagent-route t3-route artifact-route notes-route oidc-client-secret oidc-sentinel; do
	templates+=(-s "templates/$template.yaml")
done
# Install the test CA in the initial proxy revision. A later patch can roll
# the pod out from under the service port-forward during the session tests.
helm template edge "$chart" -n edge --set authentik.hostname=issuer.edge.svc.cluster.local "${templates[@]}" >"$tmp/edge.yaml"
cat >"$tmp/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - edge.yaml
patches:
  - target:
      group: gateway.envoyproxy.io
      kind: EnvoyProxy
      name: edge
    patch: |-
      - op: add
        path: /spec/provider/kubernetes/envoyDeployment/pod/volumes
        value:
          - name: ca
            configMap:
              name: issuer-ca
      - op: add
        path: /spec/provider/kubernetes/envoyDeployment/container/volumeMounts
        value:
          - name: ca
            mountPath: /etc/ssl/certs/ca-certificates.crt
            subPath: ca.pem
YAML
kubectl apply -k "$tmp"
kubectl -n edge wait gateway/edge --for=condition=Programmed --timeout=180s
kubectl -n edge rollout status deployment -l gateway.envoyproxy.io/owning-gateway-name=edge
kubectl -n edge port-forward svc/edge-proxy 18080:80 >"$tmp/forward.log" 2>&1 &
kubectl -n garage port-forward svc/garage 13900:3900 >"$tmp/garage-forward.log" 2>&1 &
for _ in {1..30}; do
	if curl -fsS -o /dev/null --aws-sigv4 aws:amz:garage:s3 --user "$SHARE_RW_ID:$SHARE_RW_SECRET" \
		-X PUT -H 'Content-Type: text/html; charset=utf-8' --data-binary '<h1>shared</h1>' \
		http://127.0.0.1:13900/bosun-artifacts/1501965852042330302/1501965852042330999/report.html; then
		break
	fi
	sleep 1
done
python3 session.py

# Admission evaluates the production socket guard against the API server.
kubectl -n t3 create serviceaccount t3-relay
kubectl apply -f "$projects/platform-factory/edge-admission/resources/policies.yaml"
cat >"$tmp/socket-probe.yaml" <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: socket-probe
  ownerReferences:
    - apiVersion: apps/v1
      kind: ReplicaSet
      name: t3-relay-test
      uid: 11111111-1111-1111-1111-111111111111
      controller: true
spec:
  serviceAccountName: t3-relay
  containers:
    - name: probe
      image: busybox
      volumeMounts:
        - name: socket
          mountPath: /socket
          readOnly: true
  volumes:
    - name: socket
      hostPath:
        path: /run/t3-cluster
        type: Directory
YAML
denied=false
for _ in {1..15}; do
	if output=$(kubectl -n t3 create --dry-run=server -f "$tmp/socket-probe.yaml" 2>&1); then
		sleep 1
	elif [[ "$output" == *'Only the t3 relay may mount'* ]]; then
		denied=true
		break
	else
		printf '%s\n' "$output" >&2
		exit 1
	fi
done
[[ "$denied" == true ]]
kubectl -n t3 create --dry-run=server -f "$tmp/socket-probe.yaml" \
	--as=system:serviceaccount:kube-system:replicaset-controller
for mount in / /run /var/run /var/run/t3-cluster/t3.sock; do
	if output=$(sed "s|path: /run/t3-cluster|path: $mount|" "$tmp/socket-probe.yaml" |
		kubectl -n t3 create --dry-run=server -f - \
			--as=system:serviceaccount:kube-system:replicaset-controller 2>&1); then
		echo "unexpected hostPath admission: $mount" >&2
		exit 1
	fi
	[[ "$output" == *'Only the t3 relay may mount'* ]]
done
