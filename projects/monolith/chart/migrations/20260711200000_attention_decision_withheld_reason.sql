-- Record WHY an ambient engage produced no in-channel reply, so the
-- /improve-ambient loop can tell the three (now four) silent paths apart
-- instead of seeing them all as a null reply_message_id. Before this, an
-- engage with reply_message_id IS NULL could be the agent-thread path, the
-- no_reply tool, the post-generation send-gate veto, or an empty/placeholder
-- reply, and the skill's before/after aggregates could not measure the gates.
--
-- Null when a reply was actually sent (reply_message_id is populated). Set to
-- one of a small fixed vocabulary otherwise:
--   'agent_thread' - routed to the goose guest, which opens its own thread
--   'no_reply'     - the model called the no_reply tool (deliberate silence)
--   'send_gate'    - the post-generation send-gate vetoed the drafted reply
--   'empty_reply'  - the model emitted no content / a bare placeholder
-- Left unconstrained (no CHECK) so a future silent path can be added without a
-- schema migration. No index: it is a low-cardinality analytic column only ever
-- grouped/filtered in aggregate queries, never joined.
ALTER TABLE chat.attention_decision
    ADD COLUMN IF NOT EXISTS withheld_reason TEXT;
