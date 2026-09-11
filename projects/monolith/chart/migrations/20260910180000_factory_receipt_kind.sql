-- Refine tasks brief an issue instead of delivering it (#6002).
ALTER TABLE swarm.factory_receipt ADD COLUMN kind TEXT;
