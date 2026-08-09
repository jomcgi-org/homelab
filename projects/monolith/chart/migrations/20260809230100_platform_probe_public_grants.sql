-- Public-tier grant for the platform probe latch. public_reader needs SELECT so
-- the public health endpoint can read the latest result. There is NO
-- public_writer grant: the writer is the private monolith's leader singleton
-- running as the app role, never the public tier.

GRANT SELECT ON platform_probe TO public_reader;
