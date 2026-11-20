/* invariant: FACT-13 */
-- Older connectors omit the marker on insert, including during rollback.
ALTER TABLE run ALTER COLUMN provenance_required SET DEFAULT false;

-- Under the former default, a coder row without a snapshot has no evidence
-- that its creator understood provenance. Preserve any existing receipts.
UPDATE run SET provenance_required = false WHERE provenance_required AND model IS NULL;
