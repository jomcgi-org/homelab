#!/bin/bash
# PreToolUse hook: warn when main.py adds a domain.on_startup_jobs(session)
# call that is not mocked in any main_*_test.py lifespan patch list.
#
# When a new domain is added to the async lifespan startup sequence in
# projects/monolith/app/main.py, every test file that patches the lifespan
# must add a corresponding patch("domain.on_startup_jobs") entry to its patch
# list, or the lifespan calls the real implementation and may fail or produce
# side-effects during tests.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr -- advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only trigger on projects/monolith/app/main.py
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

if [[ "$FILE_PATH" != */projects/monolith/app/main.py ]]; then
	exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

if [[ "$TOOL_NAME" == "Edit" ]]; then
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$CONTENT" ]]; then
	exit 0
fi

# Extract domain names from <domain>.on_startup_jobs(session) calls
DOMAINS=$(printf '%s\n' "$CONTENT" |
	grep -oE '[a-z_]+\.on_startup_jobs\(session\)' |
	sed 's/\.on_startup_jobs(session)//' |
	sort -u || true)

if [[ -z "$DOMAINS" ]]; then
	exit 0
fi

# Derive the app directory from the file path being written
APP_DIR=$(dirname "$FILE_PATH")

# Collect all domains that are patched in any test file
ALL_PATCHED=$(grep -h -oE 'patch\("[a-z_.]+\.on_startup_jobs"\)' \
	"$APP_DIR"/main_*_test.py 2>/dev/null |
	grep -oE '"[a-z_.]+\.on_startup_jobs"' |
	sed 's/"//g; s/\.on_startup_jobs//' |
	sort -u || true)

# Check each domain from main.py
MISSING=""
while IFS= read -r domain; do
	[[ -z "$domain" ]] && continue
	if ! printf '%s\n' "$ALL_PATCHED" | grep -qxF "$domain"; then
		MISSING="${MISSING}  ${domain}"$'\n'
	fi
done <<<"$DOMAINS"

if [[ -n "$MISSING" ]]; then
	cat >&2 <<-EOF
		WARNING: main.py calls on_startup_jobs for domain(s) not mocked in any test file.

		Domains missing from test patches:
		${MISSING}
		Every main_*_test.py that patches the lifespan must include
		patch("<domain>.on_startup_jobs") for each domain called in main.py,
		or tests will call the real implementation and may fail or side-effect.

		Find existing patch patterns:
		  grep -r "on_startup_jobs" projects/monolith/app/main_*_test.py

		Add the missing patch to each relevant lifespan patch list, e.g.:
		  patch("<domain>.on_startup_jobs"),
	EOF
fi

exit 0
