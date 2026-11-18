#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
chart=../../chart
[[ $(kubectl config current-context) == kind-edge-oidc ]]
for ns in edge kagent; do kubectl create namespace "$ns"; done

# Use the CRDs and controller shipped with the edge chart.
helm template crds "$chart"/charts/gateway-crds-helm-*.tgz --set crds.gatewayAPI.enabled=true --set crds.envoyGateway.enabled=true | kubectl apply --server-side -f -
helm install edge "$chart"/charts/gateway-helm-*.tgz -n edge --skip-crds --wait \
	--set crds.enabled=false --set config.envoyGateway.provider.type=Kubernetes \
	--set config.envoyGateway.provider.kubernetes.deploy.type=GatewayNamespace

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=issuer.edge.svc.cluster.local' \
	-addext 'subjectAltName=DNS:issuer.edge.svc.cluster.local' -keyout "$tmp/key.pem" -out "$tmp/ca.pem" 2>/dev/null
kubectl -n edge create configmap issuer-ca --from-file="$tmp/ca.pem"
for ns in edge kagent; do
	kubectl -n "$ns" create secret generic fixture --from-file=issuer.py --from-file="$tmp/key.pem" --from-file="$tmp/ca.pem"
	kubectl -n "$ns" create deployment fixture --image=python:3.12-bookworm -- python /test/issuer.py
	kubectl -n "$ns" patch deployment fixture --type=strategic -p '{"spec":{"template":{"spec":{"volumes":[{"name":"test","secret":{"secretName":"fixture"}}],"containers":[{"name":"python","volumeMounts":[{"name":"test","mountPath":"/test"}]}]}}}}'
	kubectl -n "$ns" rollout status deploy/fixture
done
kubectl -n edge expose deployment fixture --name issuer --port 443 --target-port 8443
kubectl -n kagent expose deployment fixture --name kagent-ui --port 8080

templates=()
for template in gateway security-policy kagent-route oidc-client-secret oidc-sentinel; do
	templates+=(-s "templates/$template.yaml")
done
helm template edge "$chart" -n edge --set authentik.hostname=issuer.edge.svc.cluster.local "${templates[@]}" | kubectl apply -f -
kubectl -n edge patch envoyproxy edge --type merge -p '{"spec":{"provider":{"kubernetes":{"envoyDeployment":{"pod":{"volumes":[{"name":"ca","configMap":{"name":"issuer-ca"}}]},"container":{"volumeMounts":[{"name":"ca","mountPath":"/etc/ssl/certs/ca-certificates.crt","subPath":"ca.pem"}]}}}}}}'
kubectl -n edge wait gateway/edge --for=condition=Programmed --timeout=180s
kubectl -n edge rollout status deployment -l gateway.envoyproxy.io/owning-gateway-name=edge
kubectl -n edge port-forward svc/edge-proxy 18080:80 >"$tmp/forward.log" 2>&1 &
for ((i = 0; i < 60; i++)); do
	if [[ $(curl -s -o /dev/null -w '%{http_code}' -H 'Host: factory.jomcgi.dev' http://localhost:18080/oauth2-sentinel) == 302 ]]; then break; fi
	sleep 1
done
python3 session.py
