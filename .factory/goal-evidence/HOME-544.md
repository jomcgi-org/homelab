## Criterion 2

The repository binding in `projects/factory/chart/values-homelab.yaml` selects
`homelab` and `codex`. The `factory.bindingAgentName` helper in
`projects/factory/chart/templates/_helpers.tpl` renders `homelab-codex`.
The pinned kagent revision e3d14ee3dd62 uses the compiled agent name as
the default service name in `go/pkg/tracing/runtime.go` (ResourceDefaults).

`projects/platform/otel-collector/values-factory.yaml` admits that service
and enables trace sanitization. The collector test
`test_factory_trace_content_removed_before_sampling` in
`projects/platform/otel-collector/collector_optin_test.py` exercises the
rendered filter and transform with sensitive span and event attributes.
