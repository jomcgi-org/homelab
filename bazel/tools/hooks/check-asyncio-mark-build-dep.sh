#!/bin/bash
# PreToolUse hook: warns when a *_test.py file uses @pytest.mark.asyncio or
# async def test_ without @pip//pytest_asyncio in the nearest BUILD file.
#
# pytest-asyncio is NOT a default dep in this repo. Without it, async tests
# decorated with @pytest.mark.asyncio are silently collected but never executed,
# giving false-green CI results.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: always (warning only — never blocks)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# No file path — nothing to check
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Only check *_test.py files
if [[ "$FILE_PATH" != *_test.py ]]; then
	exit 0
fi

# Get content being written (Write tool) or read current file (Edit tool)
CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
if [[ -z "$CONTENT" ]]; then
	# Edit tool — read current file if it exists
	if [[ -f "$FILE_PATH" ]]; then
		CONTENT=$(cat "$FILE_PATH")
	fi
fi

if [[ -z "$CONTENT" ]]; then
	exit 0
fi

# Check for asyncio test markers
if ! echo "$CONTENT" | grep -qE '(pytest\.mark\.asyncio|async[[:space:]]+def[[:space:]]+test_)'; then
	exit 0
fi

# Walk up from the test file's directory to find the nearest BUILD file
DIR=$(dirname "$FILE_PATH")
BUILD_FILE=""
SEARCH_DIR="$DIR"
while [[ -n "$SEARCH_DIR" && "$SEARCH_DIR" != "/" ]]; do
	if [[ -f "$SEARCH_DIR/BUILD" ]]; then
		BUILD_FILE="$SEARCH_DIR/BUILD"
		break
	fi
	if [[ -f "$SEARCH_DIR/BUILD.bazel" ]]; then
		BUILD_FILE="$SEARCH_DIR/BUILD.bazel"
		break
	fi
	SEARCH_DIR=$(dirname "$SEARCH_DIR")
done

# If no BUILD file found, warn anyway — the dep is definitely missing
if [[ -z "$BUILD_FILE" ]]; then
	cat >&2 <<-EOF
		WARNING: $FILE_PATH uses @pytest.mark.asyncio / async def test_ but no BUILD file was found.

		@pytest.mark.asyncio requires pytest-asyncio in the BUILD target's deps:
		  deps = ["@pip//pytest_asyncio", ...]

		Without pytest-asyncio, async tests are silently never executed (false-green CI).
		Use the asyncio.run() pattern instead:
		  def test_my_async_thing():
		      asyncio.run(my_coroutine())
	EOF
	exit 0
fi

# Check if the BUILD file includes @pip//pytest_asyncio
if grep -q '@pip//pytest_asyncio' "$BUILD_FILE"; then
	exit 0
fi

cat >&2 <<-EOF
	WARNING: $FILE_PATH uses @pytest.mark.asyncio / async def test_ but '@pip//pytest_asyncio'
	is not present in $BUILD_FILE.

	Without pytest-asyncio in deps, async tests decorated with @pytest.mark.asyncio
	are silently never executed — CI appears green but the tests never run.

	Either add the dep:
	  deps = ["@pip//pytest_asyncio", ...]

	Or use the asyncio.run() pattern instead (preferred in this repo):
	  def test_my_async_thing():
	      asyncio.run(my_coroutine())
EOF

exit 0
