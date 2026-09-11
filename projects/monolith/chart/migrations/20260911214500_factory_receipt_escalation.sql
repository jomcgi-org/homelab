-- Structured escalation options and their resolution, for #6002.
-- A needs-human refine now writes the two to four options a person may pick
-- from, and the decision endpoint writes the chosen one back into the same
-- document. One nullable column so an older receipt reads as no escalation.
ALTER TABLE swarm.factory_receipt
  ADD COLUMN escalation_json TEXT;
