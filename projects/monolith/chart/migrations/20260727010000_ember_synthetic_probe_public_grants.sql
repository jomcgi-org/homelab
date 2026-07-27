-- Public-tier grant for the synthetic-probe latch. public_reader needs SELECT
-- so the public health endpoint can read the latest result. There is NO
-- public_writer grant because the writer runs in the private jobs pod as the
-- app role, not in the public tier.

GRANT SELECT ON ember_synthetic_probe TO public_reader;
