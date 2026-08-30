import pytest

from swarm import turn_artifact as ta


SCHEMA = {
    "type": "object",
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "kind"],
                "properties": {
                    "key": {"type": "string"},
                    "kind": {"enum": ["work", "gate", "merge"]},
                },
            },
        }
    },
}


def added_file_diff(path, body):
    """A git diff for a newly created file, the shape the guest shim produces
    when an agent writes an untracked file during its turn."""
    lines = body.split("\n")
    hunk = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{hunk}\n"
    )


def modified_file_diff(path):
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        '-{"nodes": []}\n'
        '+{"nodes": [{"key": "a", "kind": "work"}]}\n'
    )


class TestExtraction:
    def test_recovers_a_freshly_written_file(self):
        body = '{\n  "nodes": []\n}'
        content, failure = ta.extract_artifact(
            added_file_diff("plan.json", body), "plan.json"
        )
        assert failure is None
        assert content == body

    def test_missing_when_the_turn_wrote_nothing(self):
        content, failure = ta.extract_artifact(None, "plan.json")
        assert content is None
        assert failure.status == ta.MISSING

    def test_missing_when_the_turn_wrote_other_files(self):
        content, failure = ta.extract_artifact(
            added_file_diff("notes.md", "hello"), "plan.json"
        )
        assert content is None
        assert failure.status == ta.MISSING

    def test_a_modified_file_is_refused_rather_than_half_parsed(self):
        """A hunk is not a document. Reconstructing from one would validate a
        fragment as though it were the whole artifact, which is the false-green
        this refusal exists to prevent."""
        content, failure = ta.extract_artifact(
            modified_file_diff("plan.json"), "plan.json"
        )
        assert content is None
        assert failure.status == ta.NOT_FRESH

    def test_a_reduced_diff_from_a_large_turn_still_yields_the_artifact(self):
        """When the full work diff busts the guest shim's size caps, the shim
        now emits a reduced diff holding only small added files (truncated stays
        True). A node whose accumulated diff is huge must still get its 200-byte
        plan.json through, or retries livelock on diff size alone."""
        body = '{"nodes": [{"key": "research", "kind": "work"}]}'
        reduced_diff = added_file_diff("plan.json", body)
        content, failure = ta.extract_artifact(reduced_diff, "plan.json")
        assert failure is None
        assert content == body
        outcome = ta.evaluate(reduced_diff, "plan.json", SCHEMA)
        assert outcome.ok


class TestEvaluate:
    def test_accepts_a_valid_document(self):
        body = '{"nodes": [{"key": "research", "kind": "work"}]}'
        outcome = ta.evaluate(added_file_diff("plan.json", body), "plan.json", SCHEMA)
        assert outcome.ok
        assert outcome.value == {"nodes": [{"key": "research", "kind": "work"}]}

    def test_unparsable_json_is_distinct_from_invalid(self):
        outcome = ta.evaluate(
            added_file_diff("plan.json", "{not json"), "plan.json", SCHEMA
        )
        assert outcome.status == ta.UNPARSABLE
        assert outcome.errors

    def test_reports_every_violation_not_just_the_first(self):
        """One error per retry would cost one guest boot per mistake."""
        body = '{"nodes": [{"kind": "wrong"}, {"key": 7, "kind": "work"}]}'
        outcome = ta.evaluate(added_file_diff("plan.json", body), "plan.json", SCHEMA)
        assert outcome.status == ta.INVALID
        assert len(outcome.errors) >= 3
        assert outcome.errors == sorted(outcome.errors)

    def test_the_rejected_document_is_kept_for_the_operator(self):
        body = '{"nodes": [{"kind": "wrong"}]}'
        outcome = ta.evaluate(added_file_diff("plan.json", body), "plan.json", SCHEMA)
        assert outcome.value == {"nodes": [{"kind": "wrong"}]}


class TestEvaluateContent:
    def test_accepts_a_valid_document(self):
        raw = '{"nodes": [{"key": "research", "kind": "work"}]}'
        outcome = ta.evaluate_content(raw, "plan.json", SCHEMA)
        assert outcome.ok
        assert outcome.value == {"nodes": [{"key": "research", "kind": "work"}]}

    def test_unparsable_json_is_distinct_from_invalid(self):
        outcome = ta.evaluate_content("{not json", "plan.json", SCHEMA)
        assert outcome.status == ta.UNPARSABLE
        assert outcome.errors

    def test_reports_sorted_schema_violations(self):
        raw = '{"nodes": [{"kind": "wrong"}, {"key": 7, "kind": "work"}]}'
        outcome = ta.evaluate_content(raw, "plan.json", SCHEMA)
        assert outcome.status == ta.INVALID
        assert len(outcome.errors) >= 3
        assert outcome.errors == sorted(outcome.errors)

    def test_none_is_missing(self):
        outcome = ta.evaluate_content(None, "plan.json", SCHEMA)
        assert outcome.status == ta.MISSING
        assert outcome.errors == ["plan.json was not delivered this turn"]

    def test_decodes_bytes_as_utf8(self):
        outcome = ta.evaluate_content(b'{"nodes": []}', "plan.json", SCHEMA)
        assert outcome.ok
        assert outcome.value == {"nodes": []}

    def test_invalid_utf8_bytes_are_unparsable(self):
        outcome = ta.evaluate_content(b'{"key": "\xff\xfe"}', "plan.json", SCHEMA)
        assert outcome.status == ta.UNPARSABLE
        assert outcome.errors

    def test_modified_file_is_not_fresh_in_diff_but_valid_when_delivered(self):
        _, diff_outcome = ta.extract_artifact(
            modified_file_diff("plan.json"), "plan.json"
        )
        delivered = b'{"nodes": [{"key": "a", "kind": "work"}]}'

        assert diff_outcome.status == ta.NOT_FRESH
        direct_outcome = ta.evaluate_content(delivered, "plan.json", SCHEMA)
        assert direct_outcome.ok


class TestRetryInstruction:
    def test_names_the_file_and_every_reason(self):
        outcome = ta.ArtifactOutcome(
            ta.INVALID, errors=["nodes/0: bad", "nodes/1: worse"]
        )
        text = ta.retry_instruction(outcome, "plan.json")
        assert "plan.json" in text
        assert "nodes/0: bad" in text
        assert "nodes/1: worse" in text


class TestLadder:
    def test_a_valid_artifact_is_accepted(self):
        assert ta.next_action(ta.ArtifactOutcome(ta.OK), 1, 2) == ta.ACCEPT

    def test_a_failure_with_budget_left_retries(self):
        assert ta.next_action(ta.ArtifactOutcome(ta.INVALID), 1, 2) == ta.RETRY

    def test_a_failure_out_of_budget_escalates(self):
        assert ta.next_action(ta.ArtifactOutcome(ta.INVALID), 2, 2) == ta.ESCALATE

    @pytest.mark.parametrize(
        "status", [ta.MISSING, ta.NOT_FRESH, ta.UNPARSABLE, ta.INVALID]
    )
    @pytest.mark.parametrize("attempts", [1, 2, 5, 99])
    def test_no_failure_mode_can_ever_strand_a_node(self, status, attempts):
        """The load-bearing guarantee: a node whose artifact never validates
        always ends up in front of the conductor, which can retry it or reshape
        the plan around it. Nothing here may return a terminal failure, so any
        new outcome status has to stay on this ladder."""
        action = ta.next_action(ta.ArtifactOutcome(status), attempts, 2)
        assert action in (ta.RETRY, ta.ESCALATE)

    def test_zero_retry_budget_escalates_immediately(self):
        """A node configured with no retries still reaches the conductor rather
        than dying where it stands."""
        assert ta.next_action(ta.ArtifactOutcome(ta.MISSING), 1, 0) == ta.ESCALATE
