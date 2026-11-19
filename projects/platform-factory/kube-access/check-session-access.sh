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
# api-resources lists no subresources. Check the readable core ones here; keep
# this list equal to the one in resources/sessions.yaml.
for subresource in namespaces/status nodes/status persistentvolumeclaims/status \
	persistentvolumes/status pods/ephemeralcontainers pods/log pods/resize \
	pods/status replicationcontrollers/scale replicationcontrollers/status \
	resourcequotas/status services/status; do
	"${session[@]}" get "${subresource%/*}" --subresource="${subresource#*/}" --all-namespaces --quiet || {
		echo "Missing cluster-wide get on $subresource" >&2
		exit 1
	}
done
"${session[@]}" -n factory get secrets/factory-itsaplan-joe-sessions --quiet
for verb in get create; do
	"${session[@]}" -n factory "$verb" pods/factory-itsaplan-session-forward-0 --subresource=portforward --quiet
done
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
expect_denied -n factory get pods/other --subresource=portforward
# A GET on these opens a command stream or a proxy: a write.
expect_denied get nodes --subresource=proxy
for subresource in exec attach proxy; do
	expect_denied -n factory get pods/other --subresource="$subresource"
done
expect_denied -n factory get services/other --subresource=proxy
