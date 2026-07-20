-- Public-tier grants for demo_sg_savings (ADR security/005's narrow-grant
-- precedent, same as 20260718010000_demo_pg_savings_public_grants.sql and
-- 20260719010000_bazel_query_savings_public_grants.sql).
--
-- public_reader gets SELECT so the cached GET /api/ember/semgrep/savings
-- endpoint (reader engine) can read the all-time counter from the replica.
--
-- public_writer gets SELECT, INSERT, UPDATE (no DELETE: the row is a
-- single-row accumulator, never removed) so a successful public POST /scan
-- can accrue its saved-time credit (ember_public.semgrep_core's write path,
-- same writer-engine pattern as demo_pg_savings and bazel_query_savings).
-- Missing this grant means public scans would 503 trying to write the
-- credit while private ones succeed, a silent split rather than an obvious
-- failure.

GRANT SELECT ON demo_sg_savings TO public_reader;
GRANT SELECT, INSERT, UPDATE ON demo_sg_savings TO public_writer;
