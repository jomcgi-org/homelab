import {
  WebTracerProvider,
  BatchSpanProcessor,
} from "@opentelemetry/sdk-trace-web";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { DocumentLoadInstrumentation } from "@opentelemetry/instrumentation-document-load";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { Resource } from "@opentelemetry/resources";
import {
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION,
} from "@opentelemetry/semantic-conventions";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

export function initTelemetry() {
  try {
    const resource = new Resource({
      [ATTR_SERVICE_NAME]: "monolith-frontend",
      [ATTR_SERVICE_VERSION]: "1.0.0",
    });

    const exporter = new OTLPTraceExporter({
      url: `${window.location.origin}/otel/v1/traces`,
    });

    const provider = new WebTracerProvider({
      resource,
      spanProcessors: [new BatchSpanProcessor(exporter)],
    });

    provider.register();

    registerInstrumentations({
      instrumentations: [
        new DocumentLoadInstrumentation(),
        new FetchInstrumentation({
          // Skip instrumenting third-party hosts that reject the CORS
          // preflight: fonts, plus the OpenFreeMap tiles behind the
          // /app/ships basemap (its OPTIONS returns 405).
          ignoreUrls: [
            /\/otel\//,
            /fonts\.googleapis/,
            /fonts\.gstatic/,
            /tiles\.openfreemap\.org/,
          ],
          // Only continue traces into same-origin requests (the safe
          // default). Propagating to every cross-origin host injects a
          // `traceparent` header, which turns the request "non-simple" and
          // forces a CORS preflight that third-party CDNs (OpenFreeMap,
          // Google Fonts) answer with a non-2xx status, blocking the fetch.
          propagateTraceHeaderCorsUrls: [],
        }),
      ],
    });
  } catch (e) {
    console.warn("Failed to initialize telemetry:", e);
  }
}
