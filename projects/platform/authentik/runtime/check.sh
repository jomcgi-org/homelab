#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -z ${FACTORY_TEST_IMAGE:-} ]]; then
	FACTORY_TEST_IMAGE=$(yq -r '.authentik.global.image | .repository + ":" + .tag + "@" + .digest' ../values-factory.yaml)
fi
export FACTORY_TEST_IMAGE
export COMPOSE_PROJECT_NAME="factory-social-test-$$"
trap 'docker compose down -v' EXIT
docker compose up -d
for ((attempt = 1; attempt <= 60; attempt++)); do
	if docker compose exec -T authentik ak shell -c 'from authentik.flows.models import Flow; assert Flow.objects.filter(slug="default-source-authentication").exists()'; then
		docker compose exec -T authentik ak shell -c 'import runpy; runpy.run_path("/fixture/runtime/social.py")'
		exit 0
	fi
	sleep 5
done
echo "Timed out waiting for the default source flow" >&2
exit 1
