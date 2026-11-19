# HOME-508 evidence

1. `projects/platform/otel-collector/values-factory.yaml` and `test_factory_renders_gateway` in `//projects/platform/otel-collector:collector_optin_test` cover admission, the Honeycomb exporter and tail sampling.
2. kagent export is a later Activation step (HOME-544). `projects/factory/chart/chart_test.py::test_binding_worker_pool_sets_resources_and_concurrency` checks that the kagent controller and runtime telemetry stay empty, and the `trace-gateway` policy in `projects/platform-factory/otel-collector/resources/network-policy.yaml` admits only the three factory service pods.
3. `docs/observability.md` contains the “Traces first” rule.
4. `projects/platform/otel-collector/values-factory.yaml` gives the sampling assumptions and 30-day event estimate.
5. `//projects/platform/otel-collector:collector_optin_test` runs `test_factory_trace_content_removed_before_sampling` against the pinned collector binary. It checks that `gen_ai` content attributes leave spans and span events, and that token counts and factory attributes remain.
