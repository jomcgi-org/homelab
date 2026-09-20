ALTER TABLE swarm.factory_start
  DROP CONSTRAINT factory_start_accounting_basis_check;
ALTER TABLE swarm.factory_start
  ADD CONSTRAINT factory_start_accounting_basis_check
  CHECK (accounting_basis IS NULL
         OR accounting_basis IN (
           'no_model_post',
           'capacity_denied',
           'no_session_created'
         ));
