#!/usr/bin/env bash
# Render a local Helm chart and compare it with the live ArgoCD application.

set -o errexit -o nounset -o pipefail

die() {
	echo "ERROR: $*" >&2
	exit 2
}

# Bazel's rootpath expansion is relative to the runfiles tree. Resolve it
# explicitly so `bazel run` works even when invoked from outside the workspace.
resolve_runfile() {
	local logical="$1" root candidate

	if [[ -e "$logical" ]]; then
		printf '%s\n' "$logical"
		return 0
	fi

	for root in "${RUNFILES_DIR:-}" "${TEST_SRCDIR:-}"; do
		[[ -n "$root" ]] || continue
		for candidate in "$root/$logical" "$root/_main/$logical"; do
			if [[ -e "$candidate" ]]; then
				printf '%s\n' "$candidate"
				return 0
			fi
		done
	done

	return 1
}

resolve_tool() {
	local configured="$1" fallback="$2" resolved

	if [[ "$configured" == */* ]]; then
		resolved=$(resolve_runfile "$configured") || return 1
		[[ -x "$resolved" ]] || return 1
		printf '%s\n' "$resolved"
		return 0
	fi

	command -v "$configured" 2>/dev/null || command -v "$fallback" 2>/dev/null
}

[[ -n "${ARGOCD_APP_NAME:-}" ]] || die "ARGOCD_APP_NAME is not set"
[[ -n "${CHART_FILE:-}" ]] || die "CHART_FILE is not set"
[[ -n "${RELEASE_NAME:-}" ]] || die "RELEASE_NAME is not set"
[[ -n "${NAMESPACE:-}" ]] || die "NAMESPACE is not set"

helm_bin=$(resolve_tool "${HELM:-helm}" helm) || die "cannot locate the Helm CLI"
argocd_bin=$(resolve_tool "${ARGOCD:-argocd}" argocd) || die "cannot locate the ArgoCD CLI"
chart_file=$(resolve_runfile "$CHART_FILE") || die "cannot locate chart file: $CHART_FILE"
chart_dir=$(cd "$(dirname "$chart_file")" && pwd -P)
chart_file="$chart_dir/$(basename "$chart_file")"

case "$chart_file" in
*/"$CHART_FILE")
	repo_root=${chart_file%/"$CHART_FILE"}
	;;
*)
	die "cannot determine repository root from chart file: $chart_file"
	;;
esac

values_args=()
while IFS= read -r values_file || [[ -n "$values_file" ]]; do
	[[ -n "$values_file" ]] || continue
	resolved_values=$(resolve_runfile "$values_file") || die "cannot locate values file: $values_file"
	values_args+=(--values "$resolved_values")
done <<<"${VALUES_FILES:-}"

tmp_parent="${TEST_TMPDIR:-${TMPDIR:-/tmp}}"
mkdir -p "$tmp_parent"
render_dir=$(mktemp -d "$tmp_parent/argocd-live-diff.XXXXXX")
trap 'rm -rf "$render_dir"' EXIT

echo "Rendering local manifests for $ARGOCD_APP_NAME"
set +e
"$helm_bin" template "$RELEASE_NAME" "$chart_dir" \
	--namespace "$NAMESPACE" \
	${values_args[@]+"${values_args[@]}"} >"$render_dir/all.yaml"
helm_status=$?
set -e
if [[ "$helm_status" -ne 0 ]]; then
	die "Helm render failed with exit status $helm_status"
fi

op_bin=""
if op_bin=$(resolve_tool "${OP:-op}" op 2>/dev/null) && "$op_bin" account list >/dev/null 2>&1; then
	access_client_id=$("$op_bin" read "op://k8s-homelab/argocd-server-auth/ACCESS_CLIENT_ID" 2>/dev/null || true)
	access_client_secret=$("$op_bin" read "op://k8s-homelab/argocd-server-auth/ACCESS_CLIENT_SECRET" 2>/dev/null || true)
	if [[ -n "$access_client_id" && -n "$access_client_secret" ]]; then
		# ARGOCD_OPTS is parsed by the pinned CLI before Cobra builds the command.
		# Keep credentials out of argv, where they would be visible in `ps`.
		access_headers="\"CF-Access-Client-Id: ${access_client_id//\"/\"\"}\",\"CF-Access-Client-Secret: ${access_client_secret//\"/\"\"}\""
		access_headers=${access_headers//\'/\'\\\'\'}
		export ARGOCD_OPTS="${ARGOCD_OPTS:+$ARGOCD_OPTS }--header '$access_headers'"
	fi
fi

echo "Comparing local manifests with live ArgoCD application $ARGOCD_APP_NAME"
set +e
"$argocd_bin" app diff "$ARGOCD_APP_NAME" \
	--local "$chart_dir" \
	--local-repo-root "$repo_root" \
	--exit-code
status=$?
set -e

case "$status" in
0)
	echo "No differences found"
	;;
1)
	echo "Differences found" >&2
	;;
*)
	echo "ArgoCD diff failed with exit status $status" >&2
	;;
esac

exit "$status"
