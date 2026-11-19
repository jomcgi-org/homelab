#!/usr/bin/env bash
# Exercise the production images and provisioner on the BuildBuddy Docker runner.
set -euo pipefail
cd "$(dirname "$0")"
root=$(cd ../resources && pwd)
tmp=$(mktemp -d)
grafana="grafana-test-$$"
mcp="grafana-mcp-test-$$"
trap 'docker rm -f "$mcp" "$grafana" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
mkdir -p "$tmp/run" "$tmp/provision/datasources"
chmod 755 "$tmp" "$tmp/run"
for key in admin-password secret-key client-secret; do
	openssl rand -hex 32 >"$tmp/run/$key"
done
cp "$root/datasources.yaml" "$tmp/provision/datasources/prometheus.yaml"
grafana_image=$(awk '/image: docker.io\/grafana\/grafana:/ {print $2; exit}' "$root/grafana.yaml")
mcp_image=$(awk '/image: docker.io\/grafana\/mcp-grafana:/ {print $2; exit}' "$root/mcp-grafana.yaml")
python_image=$(awk '/image: docker.io\/library\/python:/ {print $2; exit}' "$root/grafana.yaml")
docker run -d --name "$grafana" -v "$tmp/run:/run/grafana:ro" \
	-v "$tmp/run:/etc/grafana/oidc:ro" -v "$root/grafana.ini:/etc/grafana/grafana.ini:ro" \
	-v "$tmp/provision:/etc/grafana/provisioning:ro" "$grafana_image" >/dev/null
client=(docker run --rm --network "container:$grafana" -v "$PWD:/test:ro" -v "$root:/resources:ro" -v "$tmp:/fixture" -v "$tmp/run:/run/grafana:ro")
"${client[@]}" "$python_image" python /test/provision.py
mapfile -t args < <(sed -n '/^          args:/,/^          env:/s/^            - \(--.*\)/\1/p' "$root/mcp-grafana.yaml")
docker run -d --name "$mcp" --network "container:$grafana" --env-file "$tmp/caller.env" \
	-e GRAFANA_URL=http://127.0.0.1:3000 -e GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE=/viewer \
	-v "$tmp/viewer:/viewer:ro" --entrypoint /app/mcp-grafana "$mcp_image" "${args[@]}" >/dev/null
"${client[@]}" --env-file "$tmp/caller.env" "$python_image" sh -ec \
	'pip install -q mcp==1.26.0 && python /test/viewer_read.py'
