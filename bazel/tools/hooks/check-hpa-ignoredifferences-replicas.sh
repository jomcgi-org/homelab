#!/bin/bash
# PreToolUse hook: warn when writing/editing an HPA template or an autoscaling-
# enabled values.yaml without a corresponding /spec/replicas ignoreDifferences
# entry in the service's deploy/application.yaml.
#
# Without ignoreDifferences for /spec/replicas, ArgoCD selfHeal reverts every
# HPA scale-up back to the chart's replicas value, fighting the autoscaler.
# See commit c3a540a for the pattern this enforces.
#
# Triggers when:
#   - Writing/editing */chart/templates/hpa.yaml
#   - Writing/editing a */values.yaml whose content includes autoscaling.enabled: true
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" == "Edit" ]]; then
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

# Determine whether this edit involves HPA or autoscaling configuration.
IS_HPA_TEMPLATE=false
IS_AUTOSCALING_VALUES=false

if [[ "$FILE_PATH" == */chart/templates/hpa.yaml ]]; then
	IS_HPA_TEMPLATE=true
elif [[ "$FILE_PATH" == */values.yaml ]]; then
	# Check the content being written for autoscaling.enabled: true.
	# For Edit operations, also check the existing file on disk so a partial
	# edit (e.g. just changing the replica count) still triggers the warning
	# when autoscaling was already enabled in the file.
	if echo "$CONTENT" | grep -qE 'autoscaling:' && echo "$CONTENT" | grep -A5 'autoscaling:' | grep -qE 'enabled:\s*true'; then
		IS_AUTOSCALING_VALUES=true
	elif [[ -f "$FILE_PATH" ]] && grep -qE 'autoscaling:' "$FILE_PATH" && grep -A5 'autoscaling:' "$FILE_PATH" | grep -qE 'enabled:\s*true'; then
		IS_AUTOSCALING_VALUES=true
	fi
fi

if ! $IS_HPA_TEMPLATE && ! $IS_AUTOSCALING_VALUES; then
	exit 0
fi

# Derive the service root from the file path.
#   */chart/templates/hpa.yaml  ->  SERVICE_DIR = two levels up from hpa.yaml
#   */chart/values.yaml         ->  SERVICE_DIR = one level up from values.yaml
#   */deploy/values.yaml        ->  SERVICE_DIR = one level up from deploy/
SERVICE_DIR=""
if [[ "$FILE_PATH" == */chart/templates/hpa.yaml ]]; then
	SERVICE_DIR=$(dirname "$(dirname "$(dirname "$FILE_PATH")")")
elif [[ "$FILE_PATH" == */chart/values.yaml ]]; then
	SERVICE_DIR=$(dirname "$(dirname "$FILE_PATH")")
elif [[ "$FILE_PATH" == */deploy/values.yaml ]]; then
	SERVICE_DIR=$(dirname "$(dirname "$FILE_PATH")")
else
	exit 0
fi

APP_YAML="${SERVICE_DIR}/deploy/application.yaml"
if [[ ! -f "$APP_YAML" ]]; then
	exit 0
fi

# Check if the application.yaml already has an ignoreDifferences entry for /spec/replicas.
if grep -qE '/spec/replicas' "$APP_YAML"; then
	exit 0
fi

SERVICE_NAME=$(basename "$SERVICE_DIR")

cat >&2 <<EOF
WARNING: HPA or autoscaling detected but no ignoreDifferences for /spec/replicas in:
  $APP_YAML

Service: $SERVICE_NAME

Without this entry, ArgoCD selfHeal reverts every HPA scale-up back to the
chart's replicas value, undoing the autoscaler's work on every reconcile.

Add the following block to ${APP_YAML} under spec:

  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas

See commit c3a540a for the reference implementation (monolith backend HPA).
EOF

exit 0
