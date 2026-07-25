# Tests for the no-bare-nosemgrep semgrep rule.
#
# Bare `# nosemgrep` (no colon + rule ID) suppresses ALL rules on the line and
# cannot be audited. The rule catches these so each suppression is scoped to a
# specific rule ID.
#
# Note: semgrep-core does NOT honour inline `# nosemgrep` annotations, so the
# positive case below IS detected when scanned with semgrep-core (used in Bazel
# CI). In pysemgrep (pre-commit hooks), bare `# nosemgrep` self-suppresses the
# no-bare-nosemgrep rule; enforced by bazel semgrep tests (CI)
# hook.

# ruleid: no-bare-nosemgrep
x = 1  # nosemgrep

# ruleid: no-bare-nosemgrep
y = some_call()  # nosemgrep

# ok: no-bare-nosemgrep
z = boto3_call()  # nosemgrep: boto3-endpoint-url-missing-scheme

# ok: no-bare-nosemgrep
w = other_call()  # nosemgrep: no-bare-nosemgrep
