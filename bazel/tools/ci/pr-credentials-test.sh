#!/usr/bin/env bash
# Sourced by shared_runners_test.sh in the same inert-command harness.
source "$tools/pr-credentials.sh"
expect_failure() {
	if "$@"; then
		echo "Expected failure from $1" >&2
		return 1
	fi
}
for branch in feature main; do
	[[ "$(candidate_role pull_request 7005 "$branch")" == pr ]]
done
[[ "$(candidate_role push 0 gh-readonly-queue/main/pr-7005)" == queue ]]
[[ "$(candidate_role push 0 main)" == main ]]
for record in '' 'push x main' 'push 1 main' 'pull_request 0 main' 'pull_request -1 main' 'push 0 feature'; do
	expect_failure candidate_role $record
done
cat >"$tmp/bin/docker" <<'MOCK'
#!/usr/bin/env bash
[[ "$*" == 'login ghcr.io -u jomcgi --password-stdin' ]] || exit 99
read -r input
[[ "$input" == inert-pull ]] || exit 99
echo pull-login >>"$LOG"
MOCK
chmod +x "$tmp/bin/docker"
export DOCKER_CONFIG="$tmp/docker" GHCR_PULL_TOKEN=inert-pull
mkdir "$DOCKER_CONFIG"
unset GHCR_TOKEN DOCKERHUB_TOKEN REPO_TOKEN GITHUB_TOKEN GH_TOKEN
: >"$LOG"
candidate_pr_login pr
[[ "$(cat "$LOG")" == pull-login ]]
for token in GHCR_TOKEN DOCKERHUB_TOKEN REPO_TOKEN GITHUB_TOKEN GH_TOKEN; do
	: >"$LOG"
	(
		export "$token=inert-write"
		expect_failure candidate_pr_login pr
	)
	[[ ! -s "$LOG" ]]
done
: >"$LOG"
GHCR_PULL_TOKEN='' expect_failure candidate_pr_login pr
expect_failure candidate_pr_login queue
expect_failure candidate_pr_login main
expect_failure candidate_pr_login ''
[[ ! -s "$LOG" ]]
touch "$DOCKER_CONFIG/config.json"
expect_failure candidate_pr_login pr
rm "$DOCKER_CONFIG/config.json"
# Real temporary git repo: verify tracked formatting and new BUILD drift.
mkdir "$tmp/drift"
(
	cd "$tmp/drift"
	# Use the real git, bypassing the dispatch mock.
	PATH="${PATH#*:}"
	git init -q
	echo before >tracked
	git add tracked
	git -c user.name=test -c user.email=test@example.invalid commit -qm initial
	candidate_pr_drift pr
	echo after >tracked
	expect_failure candidate_pr_drift pr
	[[ "$(git log -1 --format=%s)" == initial ]]
	git restore tracked
	touch BUILD
	expect_failure candidate_pr_drift pr BUILD
	[[ -n "$(git ls-files --others --exclude-standard)" ]]
)
# Spoofing works: these inputs confer no trust. This is an explicit limitation.
[[ "$(candidate_role push 0 main)" == main ]]
echo 'PASS staged PR authentication and drift; role spoofing remains provider-owned'
# Execute the unchanged publication dispatch with inert commands. PR metadata,
# including a main-named head, never publishes; queue retains image/render gates.
awk '
    /# ---- 3\. deploy / { capture=1; next }
    /# After whichever image stage/ { capture=0 }
    capture { sub(/^          /, ""); print }
' "$yaml" >"$tmp/publish"
awk '
    /ON_MAIN=false/ { capture=1 }
    /# ---- 1\. central BUILD/ { capture=0 }
    capture { sub(/^          /, ""); print }
' "$yaml" >"$tmp/classify"
mkdir -p "$tmp/publish-work/bazel/images/push" "$tmp/publish-work/bazel/helm"
for script in bazel/images/push/push-changed.sh bazel/helm/write-back-versions.sh bazel/helm/ci-diff-manifests.sh; do
	cat >"$tmp/publish-work/$script" <<'MOCK'
#!/usr/bin/env bash
printf '%s %s\n' "$0" "$*" >>"$LOG"
MOCK
	chmod +x "$tmp/publish-work/$script"
done
cat >"$tmp/bin/git" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
'log -1 --format=%s') echo 'feat: fixture source' ;;
'remote set-url origin https://x-access-token:inert-main@github.com/jomcgi-org/homelab.git') echo main-remote >>"$LOG" ;;
'diff --name-only base HEAD -- '*) ;;
*) exit 99 ;;
esac
MOCK
cat >"$tmp/bin/bazel" <<'MOCK'
#!/usr/bin/env bash
[[ "$*" == 'build //bazel/images:push_all --config=ci' ]] || exit 99
echo image-build >>"$LOG"
MOCK
chmod +x "$tmp/bin/git" "$tmp/bin/bazel"
for route in pr fork-main queue main; do
	: >"$LOG"
	(
		cd "$tmp/publish-work"
		base_ref=base
		GIT_BRANCH=feature GIT_PR_NUMBER=7005 GIT_REPO_DEFAULT_BRANCH=main
		case "$route" in
		fork-main) GIT_BRANCH=main ;;
		queue)
			GIT_BRANCH=gh-readonly-queue/main/pr-7005
			GIT_PR_NUMBER=0
			;;
		main)
			GIT_BRANCH=main
			GIT_PR_NUMBER=0
			;;
		esac
		source "$tmp/classify"
		export GHCR_TOKEN=inert-main
		source "$tmp/publish"
	) >"$tmp/publish-out" 2>&1
	if [[ "$route" == main ]]; then
		grep -qF 'push-changed.sh' "$LOG"
		grep -qF 'write-back-versions.sh .chart-version-records --record-publication' "$LOG"
		grep -qxF main-remote "$LOG"
		expect_failure grep -qF image-build "$LOG"
	else
		grep -qxF image-build "$LOG"
		grep -qF ci-diff-manifests.sh "$LOG"
		expect_failure grep -qE 'main-remote|push-changed|write-back-versions' "$LOG"
	fi
done
echo 'PASS existing publication dispatch: main publishes, PR and queue only build/render'
