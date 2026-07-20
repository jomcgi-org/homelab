-- demo_sg_savings' semantics changed: saved_ms per scan is now the fixed
-- COLD_START_MS credit (the skipped daemon warm-base build) rather than a
-- delta against a removed hosted-scan-median baseline (see
-- ember_public.semgrep_core.saved_ms). The existing row only ever held test
-- scans accrued under the old semantics, so zero it rather than try to
-- reconcile mixed-semantics totals.

UPDATE demo_sg_savings SET scans = 0, actual_ms = 0, saved_ms = 0;
