#!/usr/bin/env bash
# ci-diff-manifests.sh — Render Helm manifests from main and PR, diff with dyff,
# post a PR comment with collapsible sections per changed app.
#
# Usage: ./bazel/helm/ci-diff-manifests.sh
#
# Requires: BUILDBUDDY_PULL_REQUEST_NUMBER env var (set by BuildBuddy on PR builds)
#           GHCR_TOKEN env var (for gh auth, set in BuildBuddy secrets)

set -euo pipefail

main() {

	REPO_ROOT="$(git rev-parse --show-toplevel)"
	cd "$REPO_ROOT"

	# ── Configuration ──────────────────────────────────────────────────
	COMMENT_MARKER="<!-- helm-manifest-diff -->"
	MAIN_REF="origin/main"
	TMPDIR_BASE=$(mktemp -d)
	MAIN_RENDER_DIR="$TMPDIR_BASE/main"
	PR_RENDER_DIR="$TMPDIR_BASE/pr"
	MAIN_TREE_DIR="$TMPDIR_BASE/main-tree"
	mkdir -p "$MAIN_RENDER_DIR" "$PR_RENDER_DIR" "$MAIN_TREE_DIR"

	trap 'rm -rf "$TMPDIR_BASE"' EXIT

	# ── Phase 1: Setup tools ──────────────────────────────────────────
	echo "==> Building tools..."
	# --config=ci is REQUIRED: it disables the shared ~/.cache disk cache
	# (.bazelrc `build:ci --disk_cache=`). BuildBuddy's workflow pool is
	# mixed-arch, so a plain build pulls a wrong-arch helm/dyff/gh out of that
	# cross-arch-contaminated disk cache and it dies with "cannot execute binary
	# file: Exec format error" (why the manifest comment never posted).
	#
	# Capture the whole build log instead of piping into `tail -1` (#4749):
	# one line of a bazel failure is rarely the useful line, and a failed
	# build must be reported AS a build failure, never fall through to the
	# missing-tool error below.
	TOOL_BUILD_LOG="$TMPDIR_BASE/tool-build.log"
	if ! bazel build @multitool//tools/helm @multitool//tools/dyff @multitool//tools/gh @multitool//tools/yq --config=ci >"$TOOL_BUILD_LOG" 2>&1; then
		echo "ERROR: tool build FAILED (bazel exited non-zero), full output:"
		cat "$TOOL_BUILD_LOG"
		exit 1
	fi
	tail -1 "$TOOL_BUILD_LOG"
	BAZEL_BIN="$(git rev-parse --show-toplevel)/bazel-bin"

	# Resolve tool paths from the build that just ran (#4749). Walking the
	# bazel-bin convenience symlink can miss freshly built tools when the
	# symlink points at a different output base than --config=ci wrote to,
	# which surfaced as "DYFF not found in bazel-bin" after a SUCCESSFUL
	# build. cquery reports the artifacts that build produced; its paths are
	# exec-root-relative, so resolve them against the exec root derived from
	# the symlink chain, then workspace-relative (bazel-out/... resolves via
	# the bazel-out symlink).
	EXEC_ROOT=""
	if [ -L "$BAZEL_BIN" ]; then
		# <ws>/bazel-bin -> <output_base>/execroot/<ws>/bazel-out/<cfg>/bin,
		# so the exec root sits two directory levels up the link target.
		EXEC_ROOT="$(dirname "$(dirname "$(readlink "$BAZEL_BIN")")")"
	fi

	resolve_tool() {
		local label="$1" rel
		while IFS= read -r rel; do
			[ -n "$rel" ] || continue
			case "$rel" in
			/*)
				if [ -f "$rel" ] && [ -x "$rel" ]; then
					printf '%s\n' "$rel"
					return 0
				fi
				;;
			*)
				if [ -n "$EXEC_ROOT" ] && [ -f "$EXEC_ROOT/$rel" ] && [ -x "$EXEC_ROOT/$rel" ]; then
					printf '%s\n' "$EXEC_ROOT/$rel"
					return 0
				fi
				if [ -f "$REPO_ROOT/$rel" ] && [ -x "$REPO_ROOT/$rel" ]; then
					printf '%s\n' "$REPO_ROOT/$rel"
					return 0
				fi
				;;
			esac
		done < <(bazel cquery --config=ci --output=files "$label" 2>/dev/null || true)
		return 1
	}

	# Legacy fallback, kept only as insurance in case cquery misbehaves where
	# `bazel info` already does in this environment. Same walk as pre-#4749.
	fallback_find_tool() {
		find -L "$BAZEL_BIN/external" -name "$1" -type f -perm /111 2>/dev/null | head -1
	}

	HELM=$(resolve_tool "@multitool//tools/helm" || true)
	DYFF=$(resolve_tool "@multitool//tools/dyff" || true)
	GH=$(resolve_tool "@multitool//tools/gh" || true)
	YQ=$(resolve_tool "@multitool//tools/yq" || true)
	if [ -z "$HELM" ]; then HELM=$(fallback_find_tool "helm"); fi
	if [ -z "$DYFF" ]; then DYFF=$(fallback_find_tool "dyff"); fi
	if [ -z "$GH" ]; then GH=$(fallback_find_tool "gh"); fi
	if [ -z "$YQ" ]; then YQ=$(fallback_find_tool "yq"); fi

	for tool_name in HELM DYFF GH YQ; do
		tool_path="${!tool_name}"
		if [ -z "$tool_path" ]; then
			lower_name="$(printf '%s' "$tool_name" | tr '[:upper:]' '[:lower:]')"
			echo "ERROR: $tool_name not found after a successful tool build"
			echo "(lookup failure, NOT a build failure; see #4749)"
			ls -la "$BAZEL_BIN" || true
			find -L "$BAZEL_BIN/external" -maxdepth 3 -name "*$lower_name*" || true
			echo "--- cquery said ---"
			bazel cquery --config=ci --output=files "@multitool//tools/$lower_name" || true
			exit 1
		fi
		echo "  $tool_name: $tool_path"
	done

	# Authenticate gh with GHCR_TOKEN (also used for GitHub API)
	export GH_TOKEN="${GHCR_TOKEN:-}"
	if [ -z "$GH_TOKEN" ]; then
		echo "WARNING: GHCR_TOKEN not set, PR comment will be skipped"
	fi

	# ── Phase 2: Discover applications ────────────────────────────────
	echo ""
	echo "==> Discovering ArgoCD applications..."

	# parse_app extracts helm config from an application.yaml file.
	# Outputs: release_name|chart_path|namespace|values_file1,values_file2,...
	parse_app() {
		local app_file="$1"

		# Skip multi-source apps (OCI chart references, $values refs)
		if grep -q 'sources:' "$app_file" 2>/dev/null; then
			return
		fi

		# Extract fields using grep/awk — these are simple structured YAML
		local chart_path release_name namespace
		chart_path=$(grep '^\s*path:' "$app_file" | head -1 | awk '{print $2}' | tr -d '"')
		release_name=$(grep '^\s*releaseName:' "$app_file" | head -1 | awk '{print $2}' | tr -d '"')
		namespace=$(awk '/destination:/{found=1} found && /namespace:/{print $2; exit}' "$app_file" | tr -d '"')

		# Fallback: release_name defaults to metadata.name
		if [ -z "$release_name" ]; then
			release_name=$(awk '/^metadata:/{found=1} found && /name:/{print $2; exit}' "$app_file" | tr -d '"')
		fi

		# Extract values files (lines under helm.valueFiles)
		local values_csv=""
		local in_values=false
		while IFS= read -r line; do
			if echo "$line" | grep -q 'valueFiles:'; then
				in_values=true
				continue
			fi
			if $in_values; then
				# Stop at next non-list-item line
				if echo "$line" | grep -qE '^\s*-\s'; then
					local vf
					vf=$(echo "$line" | sed 's/^[[:space:]]*-[[:space:]]*//' | tr -d '"' | xargs)
					if [ -n "$values_csv" ]; then
						values_csv="$values_csv,$vf"
					else
						values_csv="$vf"
					fi
				else
					in_values=false
				fi
			fi
		done <"$app_file"

		if [ -z "$chart_path" ] || [ -z "$namespace" ]; then
			return
		fi

		echo "${release_name}|${chart_path}|${namespace}|${values_csv}"
	}

	# Collect all apps
	APPS=()
	while IFS= read -r app_file; do
		result=$(parse_app "$app_file")
		if [ -n "$result" ]; then
			APPS+=("$result")
		fi
	done < <(find projects -name "application.yaml" \
		-not -path "*/home-cluster/*" \
		-not -path "*/charts/*" | sort)

	echo "  Found ${#APPS[@]} application(s)"

	# ── Phase 3: Render from main ─────────────────────────────────────
	echo ""
	echo "==> Rendering manifests from $MAIN_REF..."

	# Ensure we have the main ref
	git fetch origin main --quiet 2>/dev/null || true

	render_app() {
		local helm_bin="$1" output_dir="$2" tree_root="$3"
		local app_spec="$4"

		IFS='|' read -r release_name chart_path namespace values_csv <<<"$app_spec"

		local chart_full_path="$tree_root/$chart_path"
		if [ ! -d "$chart_full_path" ]; then
			echo "  SKIP $release_name (chart dir not found: $chart_path)"
			return 1
		fi

		# Build values args
		local values_args=()
		if [ -n "$values_csv" ]; then
			IFS=',' read -ra vfiles <<<"$values_csv"
			for vf in "${vfiles[@]}"; do
				# Resolve relative to chart path
				local resolved="$tree_root/$chart_path/$vf"
				if [ -f "$resolved" ]; then
					values_args+=(--values "$resolved")
				else
					echo "  WARNING: values file not found: $chart_path/$vf"
				fi
			done
		fi

		local output_file="$output_dir/${release_name}.yaml"
		local helm_stderr
		helm_stderr=$(mktemp)
		if "$helm_bin" template "$release_name" "$chart_full_path" \
			--namespace "$namespace" \
			"${values_args[@]}" \
			>"$output_file" 2>"$helm_stderr"; then
			rm -f "$helm_stderr"
			return 0
		else
			echo "  ERROR rendering $release_name:" >&2
			cat "$helm_stderr" >&2
			rm -f "$output_file" "$helm_stderr"
			return 1
		fi
	}

	# Reconstruct main tree using git show
	# We need chart dirs and values files referenced by the apps (which may be outside the chart path)
	reconstruct_main_tree() {
		for app_spec in "${APPS[@]}"; do
			IFS='|' read -r release_name chart_path namespace values_csv <<<"$app_spec"

			# Get list of files in the chart path from main
			local files
			files=$(git ls-tree -r --name-only "$MAIN_REF" -- "$chart_path" 2>/dev/null) || continue

			while IFS= read -r file; do
				local dest="$MAIN_TREE_DIR/$file"
				mkdir -p "$(dirname "$dest")"
				git show "$MAIN_REF:$file" >"$dest" 2>/dev/null || true
			done <<<"$files"

			# Also fetch values files that may be outside the chart path (e.g. ../deploy/values.yaml)
			if [ -n "$values_csv" ]; then
				IFS=',' read -ra vfiles <<<"$values_csv"
				for vf in "${vfiles[@]}"; do
					# Resolve relative path from chart_path
					local resolved
					resolved=$(cd "$REPO_ROOT" && realpath -m --relative-to=. "$chart_path/$vf" 2>/dev/null) || continue
					local dest="$MAIN_TREE_DIR/$resolved"
					if [ ! -f "$dest" ]; then
						mkdir -p "$(dirname "$dest")"
						git show "$MAIN_REF:$resolved" >"$dest" 2>/dev/null || true
					fi
				done
			fi
		done
	}

	reconstruct_main_tree

	main_ok=0
	main_fail=0
	for app_spec in "${APPS[@]}"; do
		IFS='|' read -r release_name _ _ _ <<<"$app_spec"
		if render_app "$HELM" "$MAIN_RENDER_DIR" "$MAIN_TREE_DIR" "$app_spec"; then
			main_ok=$((main_ok + 1))
		else
			main_fail=$((main_fail + 1))
		fi
	done
	echo "  Rendered: $main_ok ok, $main_fail failed/skipped"

	# ── Phase 4: Render from PR ───────────────────────────────────────
	echo ""
	echo "==> Rendering manifests from PR branch..."

	pr_ok=0
	pr_fail=0
	for app_spec in "${APPS[@]}"; do
		IFS='|' read -r release_name _ _ _ <<<"$app_spec"
		if render_app "$HELM" "$PR_RENDER_DIR" "$REPO_ROOT" "$app_spec"; then
			pr_ok=$((pr_ok + 1))
		else
			pr_fail=$((pr_fail + 1))
		fi
	done
	echo "  Rendered: $pr_ok ok, $pr_fail failed/skipped"

	# ── Phase 5: Validate no new duplicate env vars ───────────────────
	echo ""
	echo "==> Checking for duplicate env var names introduced by this PR..."

	# Kubernetes silently collapses duplicate env[].name entries within the same
	# container at apply time (last one wins), so two independently-added
	# template blocks that both declare the same var (e.g. two features each
	# adding OPENROUTER_API_KEY) render N entries but the live object only ever
	# has 1. ArgoCD then reports a permanent phantom OutOfSync (rendered has N,
	# live has 1) with operationState Succeeded, which looks like nothing is
	# wrong. That cost a multi-hour production debugging saga (PR #3158). It is
	# cheap to catch here and expensive to debug post-merge, so unlike the
	# informational diff below this is a hard failure (exit 1).
	#
	# yq eval-all combined with chained `as $x` variable bindings cross-joins
	# every document in a multi-doc file instead of scoping per document (a
	# documented yq/jq eval-all quirk, confirmed empirically while writing this
	# check), so the extraction below avoids `as` entirely and builds one JSON
	# object per matching document instead. It also avoids piping into
	# `python3 - <<HEREDOC`: the heredoc redirect claims stdin outright and
	# silently discards piped input, so the python source is captured into a
	# variable first and passed via `-c`, leaving stdin free for the piped data.
	YQ_ENV_EXTRACT='
		[.] | .[] |
		select(.kind == "Deployment" or .kind == "StatefulSet" or .kind == "DaemonSet" or .kind == "Job" or .kind == "CronJob") |
		{"kind": .kind, "name": .metadata.name, "containers": (((.spec.template.spec // .spec.jobTemplate.spec.template.spec // {}).containers // []) + ((.spec.template.spec // .spec.jobTemplate.spec.template.spec // {}).initContainers // []))}
	'

	# DUP_ENV_PY reads the newline-delimited JSON produced by YQ_ENV_EXTRACT and
	# prints one "kind<TAB>name<TAB>container<TAB>dupNames" line per container
	# that has a repeated env[].name. stdlib-only: yq does the YAML parsing
	# (multi-doc, kind-aware pod-spec paths), python3's json module does the rest.
	read -r -d '' DUP_ENV_PY <<-'PYEOF' || true
		import json
		import sys

		for line in sys.stdin:
		    line = line.strip()
		    if not line:
		        continue
		    doc = json.loads(line)
		    for container in doc.get("containers") or []:
		        names = [e.get("name") for e in (container.get("env") or []) if e.get("name")]
		        dupes = []
		        for name in names:
		            if names.count(name) > 1 and name not in dupes:
		                dupes.append(name)
		        if dupes:
		            print("\t".join([doc["kind"], doc["name"], container.get("name") or "<unnamed>", ",".join(dupes)]))
	PYEOF

	find_duplicate_envs() {
		local manifest_file="$1"
		[ -f "$manifest_file" ] || return 0
		"$YQ" eval-all -o=json -I=0 "$YQ_ENV_EXTRACT" "$manifest_file" 2>/dev/null | python3 -c "$DUP_ENV_PY"
	}

	env_check_failed=0
	for app_spec in "${APPS[@]}"; do
		IFS='|' read -r release_name _ _ _ <<<"$app_spec"

		pr_file="$PR_RENDER_DIR/${release_name}.yaml"
		main_file="$MAIN_RENDER_DIR/${release_name}.yaml"

		pr_dupes=$(find_duplicate_envs "$pr_file") || true
		[ -z "$pr_dupes" ] && continue

		# Duplicates already present on main are pre-existing, not this PR's
		# fault: don't fail on those. A missing main_file (new app) naturally
		# yields an empty set here, so every duplicate in a new app's render
		# still fails. Scoped at the container level (kind+name+container) on
		# purpose, matching the instruction to keep this simple: a container
		# already flagged on main stays ignored even if the PR's duplicate is a
		# different variable name in that same container.
		main_dupes=$(find_duplicate_envs "$main_file") || true
		main_keys=$(printf '%s\n' "$main_dupes" | cut -f1-3)

		while IFS=$'\t' read -r kind name container dup_names; do
			[ -z "$kind" ] && continue
			key=$(printf '%s\t%s\t%s' "$kind" "$name" "$container")
			if ! grep -qxF "$key" <<<"$main_keys"; then
				env_check_failed=1
				echo ""
				echo "ERROR: duplicate env var name(s) in PR-rendered manifest"
				echo "  app:        $release_name"
				echo "  kind/name:  $kind/$name"
				echo "  container:  $container"
				echo "  duplicated: $dup_names"
				echo "  Kubernetes collapses duplicate env[].name entries at apply time (last"
				echo "  one wins), so this renders N entries but the live object only gets 1."
				echo "  Rename or remove one of the duplicate declarations."
			fi
		done <<<"$pr_dupes"
	done

	if [ "$env_check_failed" -eq 1 ]; then
		echo ""
		echo "==> Duplicate env var check FAILED. Fix the duplicate(s) above before merging."
		exit 1
	fi
	echo "  No new duplicate env var names introduced."

	# ── Phase 6: Diff and post comment ────────────────────────────────
	echo ""
	echo "==> Comparing manifests..."

	DIFF_BODY=""
	CHANGED_COUNT=0
	TOTAL_COUNT=0

	# Blank out helm-template-nondeterministic fields so the diff shows only real
	# config changes. Charts like linkerd mint webhook TLS certs at TEMPLATE time
	# via genSignedCert/genCA, so every render produces different cert material
	# and the dependent checksum/* annotations churn too; these renders are never
	# applied (the operator regenerates certs at deploy). Redacting also keeps
	# key-looking material out of the public PR comment. GNU sed (CI is Linux).
	# Assumes single-line base64 values (what `genSignedCert | b64enc` emits); a
	# YAML block-scalar cert would leak continuation lines, but no chart here uses
	# that form.
	redact_volatile() {
		local f="$1"
		[ -f "$f" ] || return 0
		sed -E -i \
			-e 's#^([[:space:]]*(tls\.crt|tls\.key|ca\.crt|caBundle):[[:space:]]*).+#\1<redacted>#' \
			-e 's#^([[:space:]]*"?checksum/[A-Za-z0-9._-]+"?:[[:space:]]*).+#\1<redacted>#' \
			"$f"
	}

	for app_spec in "${APPS[@]}"; do
		IFS='|' read -r release_name _ _ _ <<<"$app_spec"

		main_file="$MAIN_RENDER_DIR/${release_name}.yaml"
		pr_file="$PR_RENDER_DIR/${release_name}.yaml"

		# Handle cases where one side failed to render
		if [ ! -f "$main_file" ] && [ ! -f "$pr_file" ]; then
			continue
		fi

		# Normalize volatile fields before any diff or excerpt.
		redact_volatile "$main_file"
		redact_volatile "$pr_file"

		TOTAL_COUNT=$((TOTAL_COUNT + 1))

		if [ ! -f "$main_file" ]; then
			CHANGED_COUNT=$((CHANGED_COUNT + 1))
			DIFF_BODY+="<details>
<summary><code>${release_name}</code> — new application</summary>

\`\`\`yaml
$(head -50 "$pr_file")
\`\`\`
*(truncated — showing first 50 lines)*

</details>

"
			continue
		fi

		if [ ! -f "$pr_file" ]; then
			CHANGED_COUNT=$((CHANGED_COUNT + 1))
			DIFF_BODY+="<details>
<summary><code>${release_name}</code> — removed application</summary>

Application was removed or failed to render on the PR branch.

</details>

"
			continue
		fi

		# Run dyff
		local_diff=$("$DYFF" between --omit-header "$main_file" "$pr_file" 2>/dev/null) || true

		if [ -n "$local_diff" ]; then
			CHANGED_COUNT=$((CHANGED_COUNT + 1))
			echo "  CHANGED: $release_name"
			DIFF_BODY+="<details>
<summary><code>${release_name}</code></summary>

\`\`\`diff
${local_diff}
\`\`\`

</details>

"
		fi
	done

	echo ""
	echo "  $CHANGED_COUNT of $TOTAL_COUNT application(s) have manifest changes"

	# Build the full comment
	if [ "$CHANGED_COUNT" -eq 0 ]; then
		COMMENT="${COMMENT_MARKER}
## Helm Manifest Diff

No manifest changes detected across $TOTAL_COUNT application(s)."
	else
		COMMENT="${COMMENT_MARKER}
## Helm Manifest Diff

**$CHANGED_COUNT** of **$TOTAL_COUNT** application(s) have manifest changes.

${DIFF_BODY}"
	fi

	# Post or update PR comment. BUILDBUDDY_PULL_REQUEST_NUMBER is unset in this
	# action's env (the other reason the comment never posted), so fall back to
	# resolving the open PR from the checked-out branch via gh.
	PR_NUMBER="${BUILDBUDDY_PULL_REQUEST_NUMBER:-}"
	if [ -z "$PR_NUMBER" ] && [ -n "$GH_TOKEN" ]; then
		PR_NUMBER=$("$GH" pr list --head "$(git rev-parse --abbrev-ref HEAD)" \
			--state open --json number --jq '.[0].number // empty' 2>/dev/null || true)
	fi
	if [ -z "$PR_NUMBER" ]; then
		echo ""
		echo "No PR number found (not a PR build?). Printing diff to stdout:"
		echo ""
		echo "$COMMENT"
		exit 0
	fi

	if [ -z "$GH_TOKEN" ]; then
		echo ""
		echo "No GH_TOKEN — skipping PR comment. Diff output:"
		echo ""
		echo "$COMMENT"
		exit 0
	fi

	echo ""
	echo "==> Posting PR comment..."

	# Find existing comment by marker
	EXISTING_COMMENT_ID=$("$GH" api \
		"repos/{owner}/{repo}/issues/${PR_NUMBER}/comments" \
		--paginate \
		--jq ".[] | select(.body | startswith(\"$COMMENT_MARKER\")) | .id" \
		2>/dev/null | head -1) || true

	if [ -n "$EXISTING_COMMENT_ID" ]; then
		echo "  Updating existing comment $EXISTING_COMMENT_ID"
		"$GH" api \
			"repos/{owner}/{repo}/issues/comments/${EXISTING_COMMENT_ID}" \
			--method PATCH \
			--field body="$COMMENT" \
			--silent
	else
		echo "  Creating new comment"
		"$GH" pr comment "$PR_NUMBER" --body "$COMMENT"
	fi

	echo "Done!"

}

# Non-blocking — this is informational only, never fail the CI build
main || echo "Manifest diff failed (non-fatal)"
