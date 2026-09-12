-- A start whose attempt is proven never to have reached a model commits
-- nothing. The graph already books those runs at zero (#6045), and a start
-- ledger that still charged its reserved ceiling would refuse the retry the
-- graph just released: `turns_used` counts one row per start and
-- `authorize_start` refuses `turn_limit` at the derived allowance, which for a
-- refine node is exactly its two attempts. `accounting_basis` records which
-- proof the conductor settled the attempt on, so the accounting stays a pure
-- read of these rows. NULL is every start settled before this evidence
-- existed, and every start that did reach its model.
ALTER TABLE swarm.factory_start
  ADD COLUMN accounting_basis TEXT;
ALTER TABLE swarm.factory_start
  ADD CONSTRAINT factory_start_accounting_basis_check
  CHECK (accounting_basis IS NULL
         OR accounting_basis IN ('no_model_post', 'capacity_denied'));
