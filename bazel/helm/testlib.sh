# Shared assertions for the bazel/helm shell tests. The sourcing test owns
# the failures counter and must set failures=0 first.

# expect_status <description> <want> <got>
expect_status() {
	local description="$1" want="$2" got="$3"
	if [[ "$want" == "$got" ]]; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, want $want, got $got" >&2
		failures=$((failures + 1))
	fi
}

# expect_contains <description> <text> <file>
expect_contains() {
	local description="$1" text="$2" file="$3"
	if grep -Fq -- "$text" "$file"; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, missing '$text' in $file" >&2
		failures=$((failures + 1))
	fi
}
