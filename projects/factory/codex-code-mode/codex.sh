#!/bin/sh
# kagent validates --version, then starts app-server with its own strict config.
set -eu
if [ "${1:-}" = app-server ]; then
	set -- -c 'model_catalog_json="/etc/codex/trial-models.json"' \
		-c 'web_search="disabled"' \
		-c "features.code_mode_only=${FACTORY_CODE_MODE:-false}" "$@"
fi
exec /usr/local/libexec/codex/codex "$@"
