-- Public-tier grants for bazel_query_savings (ADR security/005's
-- narrow-grant precedent, same as 20260718010000_demo_pg_savings_public_grants.sql).
--
-- public_reader gets SELECT so the cached GET /api/ember/bazel/savings
-- endpoint (reader engine) can read the all-time counter from the replica.
--
-- public_writer gets SELECT, INSERT, UPDATE (no DELETE: the row is a
-- single-row accumulator, never removed) so a successful public POST /query
-- can accrue its skipped-analysis credit (ember_public.bazel_core's write
-- path, same writer-engine pattern as demo_pg_savings). Missing this grant
-- means public queries would 503 trying to write the credit while private
-- ones succeed, a silent split rather than an obvious failure.

GRANT SELECT ON bazel_query_savings TO public_reader;
GRANT SELECT, INSERT, UPDATE ON bazel_query_savings TO public_writer;
