#!/usr/bin/env bash
# obsidian_readiness_probe_test.sh - Validates the obsidian sidecar readiness probe
# configuration in the monolith Helm chart.
#
# Usage: obsidian_readiness_probe_test.sh <helm-binary> <chart-tgz> <entrypoint-sh>
#
# Verifies:
#   1. The obsidian container is absent when knowledge.enabled=false (default)
#   2. The obsidian container is present when knowledge.enabled=true
#   3. The readiness probe uses exec (not httpGet/tcpSocket)
#   4. The probe command is: test -f /tmp/ready
#   5. initialDelaySeconds is 5
#   6. periodSeconds is 10
#   7. The /tmp/ready sentinel path is consistent with entrypoint.sh

set -euo pipefail

if [[ $# -lt 3 ]]; then
	echo "Usage: $0 <helm-binary> <chart-tgz> <entrypoint-sh>"
	exit 1
fi

HELM="$1"
CHART_TGZ="$2"
ENTRYPOINT="$3"

PASSED=0
FAILED=0

pass() {
	echo "PASSED: $*"
	PASSED=$((PASSED + 1))
}

fail() {
	echo "FAILED: $*"
	FAILED=$((FAILED + 1))
}

# ---------------------------------------------------------------------------
# Test 1: obsidian container is absent when knowledge.enabled=false (default)
# ---------------------------------------------------------------------------
echo "--- Test 1: obsidian container absent when knowledge.enabled=false ---"
RENDERED_DEFAULT=$("$HELM" template monolith "$CHART_TGZ")
if echo "$RENDERED_DEFAULT" | grep -q "name: obsidian"; then
	fail "obsidian container should NOT be present when knowledge.enabled=false"
else
	pass "obsidian container absent when knowledge.enabled=false (default)"
fi

# Render with knowledge AND the obsidian sidecar enabled for the remaining
# tests. As of ADR 006 the sidecar is opt-in (headlessSync.enabled defaults
# false), so both flags are needed to render it.
RENDERED=$("$HELM" template monolith "$CHART_TGZ" \
	--set knowledge.enabled=true \
	--set knowledge.headlessSync.enabled=true \
	--set knowledge.headlessSync.vaultName=test-vault)

# ---------------------------------------------------------------------------
# Test 2: obsidian container is present when knowledge.enabled=true
# ---------------------------------------------------------------------------
echo "--- Test 2: obsidian container present when knowledge.enabled=true ---"
if echo "$RENDERED" | grep -q "name: obsidian"; then
	pass "obsidian container present when knowledge.enabled=true"
else
	fail "obsidian container NOT found when knowledge.enabled=true"
fi

# ---------------------------------------------------------------------------
# Test 3: readiness probe uses exec (not httpGet or tcpSocket)
# ---------------------------------------------------------------------------
echo "--- Test 3: obsidian readiness probe type is exec ---"
# The obsidian container section should contain 'exec:' in its readinessProbe
# Verify by checking exec appears after the obsidian container definition.
# We grep the section between 'name: obsidian' and the next container or volume block.
if echo "$RENDERED" | grep -A 30 "name: obsidian" | grep -q "exec:"; then
	pass "obsidian readiness probe uses exec type"
else
	fail "obsidian readiness probe exec type NOT found"
fi

# ---------------------------------------------------------------------------
# Test 4: probe command is ["test", "-f", "/tmp/ready"]
# ---------------------------------------------------------------------------
echo "--- Test 4: probe command contains 'test -f /tmp/ready' ---"
OBSIDIAN_SECTION=$(echo "$RENDERED" | grep -A 40 "name: obsidian")

# Check for each element of the command (helm may render as flow or block sequence)
if echo "$OBSIDIAN_SECTION" | grep -qE '"test"|^[[:space:]]+- test$'; then
	pass "probe command includes 'test'"
else
	fail "probe command 'test' NOT found in obsidian container section"
fi

if echo "$OBSIDIAN_SECTION" | grep -qE '"-f"|^[[:space:]]+-[[:space:]]+-f$|^[[:space:]]+"?-f"?$'; then
	pass "probe command includes '-f' flag"
else
	fail "probe command '-f' flag NOT found in obsidian container section"
fi

if echo "$OBSIDIAN_SECTION" | grep -q "/tmp/ready"; then
	pass "probe command references /tmp/ready sentinel"
else
	fail "probe command /tmp/ready sentinel NOT found in obsidian container section"
fi

# ---------------------------------------------------------------------------
# Test 5: initialDelaySeconds is 5
# ---------------------------------------------------------------------------
echo "--- Test 5: obsidian readiness probe initialDelaySeconds=5 ---"
if echo "$OBSIDIAN_SECTION" | grep -q "initialDelaySeconds: 5"; then
	pass "obsidian readiness probe initialDelaySeconds: 5"
else
	fail "obsidian readiness probe initialDelaySeconds: 5 NOT found"
fi

# ---------------------------------------------------------------------------
# Test 6: periodSeconds is 10
# ---------------------------------------------------------------------------
echo "--- Test 6: obsidian readiness probe periodSeconds=10 ---"
if echo "$OBSIDIAN_SECTION" | grep -q "periodSeconds: 10"; then
	pass "obsidian readiness probe periodSeconds: 10"
else
	fail "obsidian readiness probe periodSeconds: 10 NOT found"
fi

# ---------------------------------------------------------------------------
# Test 7: /tmp/ready sentinel path is consistent with entrypoint.sh
# ---------------------------------------------------------------------------
echo "--- Test 7: /tmp/ready sentinel path consistent with entrypoint.sh ---"
if grep -q "/tmp/ready" "$ENTRYPOINT"; then
	pass "entrypoint.sh references /tmp/ready sentinel (consistent with deployment.yaml)"
else
	fail "entrypoint.sh does NOT reference /tmp/ready — sentinel path mismatch!"
fi

# ---------------------------------------------------------------------------
# Test 8: .sync-ready sentinel written to shared vault volume
# ---------------------------------------------------------------------------
echo "--- Test 8: .sync-ready sentinel written to shared vault volume ---"
if grep -q '\.sync-ready' "$ENTRYPOINT"; then
	pass "entrypoint.sh writes .sync-ready sentinel to vault volume"
else
	fail "entrypoint.sh does NOT write .sync-ready — knowledge handlers will never run!"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASSED passed, $FAILED failed"
if [[ $FAILED -eq 0 ]]; then
	echo "All tests passed."
fi
exit "$FAILED"
