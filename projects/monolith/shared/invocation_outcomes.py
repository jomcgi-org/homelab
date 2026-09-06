"""Outcome markers shared by durable execution owners across domains.

Owners persist this marker when they cannot establish an invocation's result.
It requires reconciliation before automatic execution can resume.
"""

UNKNOWN_INVOCATION = "invocation_outcome_unknown"
