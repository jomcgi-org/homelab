import { initTelemetry } from "$lib/telemetry.js";

// The isolated table has no collector. Keep local browser runs independent
// of telemetry instrumentation; normal app builds retain their exporter.
if (import.meta.env.VITE_GRIMOIRE_LOCAL !== "true") initTelemetry();
