-- Correlate the latest synthetic result with its distributed trace and, for
-- session probes, the exact EmberVM guest allocated during that run. Nullable
-- columns preserve existing rows and no-tracing configurations.

ALTER TABLE ember_synthetic_probe
    ADD COLUMN trace_id TEXT,
    ADD COLUMN ember_session_id TEXT;
