#!/bin/bash
# PreToolUse hook: warn when writing a values.yaml with a CronWorkflow entry
# that uses a custom serviceAccountName (anything other than "argo-workflow").
#
# Argo Workflows executor pods need permission to create/patch
# workflowtaskresults in the workflow namespace. The default SA
# "argo-workflow" already has this Role binding. A custom SA that lacks it
# causes the workflow to be marked Error even when the main container exits 0,
# because the executor cannot report the result back to the controller.
# This was the root cause discovered in PR #2809.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr)
# Exit 2: block (not used -- this is advisory only)

set -euo pipefail

INPUT=$(cat)

# Only trigger on values.yaml files
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

BASENAME=$(basename "$FILE_PATH")
if [[ "$BASENAME" != "values.yaml" ]]; then
	exit 0
fi

# Get the content being written (Write tool) or the replacement string (Edit tool)
NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // .tool_input.content // empty')
if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Skip if no schedule: key present (not a CronWorkflow context)
if ! echo "$NEW_CONTENT" | grep -q 'schedule:'; then
	exit 0
fi

# Skip if no serviceAccountName: key present
if ! echo "$NEW_CONTENT" | grep -q 'serviceAccountName:'; then
	exit 0
fi

# Extract the serviceAccountName value(s) and check for any non-default SA
CUSTOM_SA=$(echo "$NEW_CONTENT" | grep -E '^\s*serviceAccountName:\s*' | grep -v 'serviceAccountName:\s*argo-workflow\s*$' || true)

if [[ -n "$CUSTOM_SA" ]]; then
	cat >&2 <<-EOF
		WARNING: CronWorkflow in values.yaml uses a custom serviceAccountName (not "argo-workflow").

		Custom service accounts need a Role granting workflowtaskresults create/patch
		in the workflow namespace. Without it, Argo marks workflows as Error even when
		the main container exits 0, because the executor cannot report results back to
		the controller (see PR #2809).

		Ensure the namespace has a Role + RoleBinding similar to:

		  apiVersion: rbac.authorization.k8s.io/v1
		  kind: Role
		  metadata:
		    name: <your-sa>-executor
		    namespace: <workflow-namespace>
		  rules:
		    - apiGroups: [argoproj.io]
		      resources: [workflowtaskresults]
		      verbs: [create, patch]
		  ---
		  apiVersion: rbac.authorization.k8s.io/v1
		  kind: RoleBinding
		  metadata:
		    name: <your-sa>-executor
		    namespace: <workflow-namespace>
		  subjects:
		    - kind: ServiceAccount
		      name: <your-sa>
		      namespace: <workflow-namespace>
		  roleRef:
		    kind: Role
		    name: <your-sa>-executor
		    apiGroup: rbac.authorization.k8s.io

		If the Role/RoleBinding is already in place, you can ignore this warning.
	EOF
fi

exit 0
