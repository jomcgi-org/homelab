-- Plan-derived task allowance under the factory policy envelope (#5419).
ALTER TABLE swarm.factory_receipt ADD COLUMN allowance_json TEXT;
