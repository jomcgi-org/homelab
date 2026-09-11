-- ADR agents/038 task class: verification mode and implementer floor (#6002).
ALTER TABLE swarm.factory_receipt ADD COLUMN task_class TEXT;
