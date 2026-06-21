# Tests for the no-bare-nosemgrep semgrep rule.
#
# Bare `# nosemgrep` (no colon + rule ID) suppresses ALL rules on the line and
# cannot be audited. The rule catches these so each suppression is scoped to a
# specific rule ID.

# ruleid: no-bare-nosemgrep
x = 1  # nosemgrep

# ruleid: no-bare-nosemgrep
y = some_call()  # nosemgrep

# ok: no-bare-nosemgrep
z = boto3_call()  # nosemgrep: boto3-endpoint-url-missing-scheme

# ok: no-bare-nosemgrep
w = other_call()  # nosemgrep: no-bare-nosemgrep
