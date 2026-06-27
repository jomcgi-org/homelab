-- Adds the work-assignment columns to claude_agent.agent_threads (ADR 022,
-- Task 3). A thread carries the job the controller hands to the guest microVM
-- over vsock: `recipe` is the goose recipe name to run (the harness image's
-- default is "agent") and `task` is the natural-language task description.
--
-- Both are nullable: legacy rows predate the assignment, and a resume (wake)
-- does not re-supply them, so the controller treats absent values as "reuse the
-- guest's existing state" rather than a new assignment.

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN recipe TEXT;

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN task TEXT;
