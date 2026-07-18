-- Public-tier grants for demo_pg_savings (ADR security/005's narrow-grant
-- precedent: public_reader/public_writer get exactly the verbs their access
-- pattern needs on exactly this table, not schema-wide access).
--
-- public_reader gets SELECT so the cached GET /api/ember/postgres/savings
-- endpoint (reader engine, per Task 3) can read the all-time counter from
-- the replica.
--
-- public_writer gets SELECT, INSERT, UPDATE (no DELETE: the row is a
-- single-row accumulator, never removed) so the status poll's accrual write
-- (ember_public.core.record_demo_pg_savings_core) can run on the public
-- tier. The credit rule in 20260718000000_demo_pg_savings.sql only accrues
-- across two consecutive samples it has itself observed in the SAME banked
-- generation; it never backfills a gap it did not witness. If public status
-- polls could read but not write this row, every public-only stretch (the
-- demo idle with no private-tier poll in the same window) would go
-- uncredited and the counter would silently stop moving whenever public
-- traffic was the only traffic. The write path must run on both tiers for
-- the counter to be a true all-time total rather than a private-tier-only
-- one.

GRANT SELECT ON demo_pg_savings TO public_reader;
GRANT SELECT, INSERT, UPDATE ON demo_pg_savings TO public_writer;
