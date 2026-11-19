#!/usr/bin/env bash
# Verify authorization without fetching Secret data or opening a tunnel.
set -euo pipefail
session=(kubectl "$@" auth can-i)
for verb in get list watch; do
	resources=$(kubectl "$@" api-resources --verbs="$verb" -o name)
	[[ -n $resources ]]
	for resource in $resources; do
		[[ $resource == secrets ]] && continue
		"${session[@]}" "$verb" "$resource" --all-namespaces --quiet || {
			echo "Missing cluster-wide $verb on $resource" >&2
			exit 1
		}
	done
done
"${session[@]}" -n factory get secrets/factory-itsaplan-joe-sessions --quiet
"${session[@]}" -n factory create pods/factory-itsaplan-session-forward-0 --subresource=portforward --quiet
expect_denied() {
	local answer
	answer=$("${session[@]}" "$@") && return 1
	[[ $answer == no ]]
}
expect_denied -n factory list secrets
expect_denied -n factory watch secrets
expect_denied -n default get secrets/factory-itsaplan-joe-sessions
expect_denied -n factory get secrets/other
expect_denied -n factory patch deployments.apps
expect_denied -n factory create pods/other --subresource=exec
expect_denied -n factory create pods/other --subresource=portforward
expect_denied -n default create pods/factory-itsaplan-session-forward-0 --subresource=portforward
