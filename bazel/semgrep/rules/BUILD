# gazelle:semgrep disabled
# gazelle:exclude golang
# gazelle:exclude python
# gazelle:exclude typescript
# gazelle:exclude yaml

load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

package(default_visibility = ["//visibility:public"])

filegroup(
    name = "shell_rules",
    srcs = glob(["shell/*.yaml"]),
)

filegroup(
    name = "bazel_rules",
    srcs = glob(["bazel/*.yaml"]),
)

filegroup(
    name = "dockerfile_rules",
    srcs = glob(["dockerfile/*.yaml"]),
)

filegroup(
    name = "yaml_rules",
    srcs = glob(
        ["yaml/*.yaml"],
        allow_empty = True,
        # memory-request-ne-limit is path-scoped to **/deploy/values.yaml and
        # **/chart/values.yaml. Excluded here to prevent yaml_rules_test from
        # running it against yaml_fixtures (which include require-resource-limits.yaml
        # with intentional request!=limit mismatches). Tested via its own target below.
        exclude = ["yaml/memory-request-ne-limit.yaml"],
    ),
)

filegroup(
    name = "kubernetes_rules",
    srcs = glob(
        ["kubernetes/*.yaml"],
        allow_empty = True,
    ) + ["@semgrep_pro_rules_kubernetes//:rules"],
)

filegroup(
    name = "python_rules",
    srcs = glob(
        ["python/*.yaml"],
        # Excluded: path-scoped rules that cannot run against all project files
        # because semgrep-core ignores paths: filters (pysemgrep-only feature).
        # These rules are wired to dedicated semgrep_test targets below.
        exclude = [
            "python/migration-destructive-ddl.yaml",
        ],
    ) + ["@semgrep_pro_rules_python//:rules"],
)

filegroup(
    name = "golang_rules",
    srcs = glob(
        ["golang/*.yaml"],
        allow_empty = True,
    ) + ["@semgrep_pro_rules_golang//:rules"],
)

# No local Rust rules yet — the repo has no Rust sources. This filegroup exists
# for parity with the other languages and carries only the Pro p/rust pack, which
# the semgrep-scand MCP scanner uses against agent-generated Rust code.
filegroup(
    name = "rust_rules",
    srcs = glob(
        ["rust/*.yaml"],
        allow_empty = True,
    ) + ["@semgrep_pro_rules_rust//:rules"],
)

filegroup(
    name = "typescript_rules",
    srcs = glob(
        ["typescript/*.yaml"],
        allow_empty = True,
    ),
)

filegroup(
    name = "javascript_rules",
    srcs = [
        ":typescript_rules",
        "@semgrep_pro_rules_javascript//:rules",
    ],
)

filegroup(
    name = "sca_golang_rules",
    srcs = ["@semgrep_sca_rules_golang//:rules"],
)

filegroup(
    name = "sca_python_rules",
    srcs = ["@semgrep_sca_rules_python//:rules"],
)

filegroup(
    name = "sca_javascript_rules",
    srcs = ["@semgrep_sca_rules_javascript//:rules"],
)

filegroup(
    name = "sca_rules",
    srcs = [
        ":sca_golang_rules",
        ":sca_javascript_rules",
        ":sca_python_rules",
    ],
)

filegroup(
    name = "generic_rules",
    srcs = glob(
        ["generic/*.yaml"],
        allow_empty = True,
    ),
)

filegroup(
    name = "sql_rules",
    srcs = glob(
        ["sql/*.yaml"],
        allow_empty = True,
    ),
)

filegroup(
    name = "all_rules",
    srcs = glob(["**/*.yaml"]) + [
        "@semgrep_pro_rules_golang//:rules",
        "@semgrep_pro_rules_javascript//:rules",
        "@semgrep_pro_rules_kubernetes//:rules",
        "@semgrep_pro_rules_python//:rules",
        "@semgrep_pro_rules_rust//:rules",
    ],
)

# Local rule files only (no Pro packs). Used by the semgrep-guest image to lay the
# repo's own rules under /etc/semgrep/rules/<subdir>/ preserving the per-language
# subdir structure (basenames collide across subdirs, so the tree must be kept).
filegroup(
    name = "local_rules",
    srcs = glob(
        [
            "**/*.yaml",
            "**/*.json",
        ],
        allow_empty = True,
    ),
)

semgrep_test(
    name = "kubernetes_rules_test",
    srcs = ["//bazel/semgrep/tests:kubernetes_yaml_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":kubernetes_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "yaml_rules_test",
    srcs = ["//bazel/semgrep/tests:yaml_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":yaml_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "golang_rules_test",
    srcs = glob(["golang/*.go"]),
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":golang_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "python_rules_test",
    srcs = glob(["python/*.py"]),
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":python_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "typescript_rules_test",
    srcs = ["//bazel/semgrep/tests:typescript_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":typescript_rules"],
    tags = ["semgrep"],
)

# no-create-event-dispatcher-runes uses languages: [generic] scoped to projects/**/*.svelte.
# Wired to its own .svelte annotation fixture independently of typescript_rules_test
# (which only scans *.ts fixtures).
filegroup(
    name = "no_create_event_dispatcher_runes_rule",
    srcs = ["typescript/no-create-event-dispatcher-runes.yaml"],
)

semgrep_test(
    name = "no_create_event_dispatcher_runes_test",
    srcs = ["//bazel/semgrep/tests:no_create_event_dispatcher_runes_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_create_event_dispatcher_runes_rule"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "generic_rules_test",
    srcs = ["//bazel/semgrep/tests:no_stale_repo_paths_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":generic_rules"],
    tags = ["semgrep"],
)

# no-generic-test-filename uses languages: [generic] so its fixture is a Python file.
# This test wires the rule to its annotation fixture independently of
# generic_rules_test (which only scans no-stale-repo-paths fixtures).
filegroup(
    name = "no_generic_test_filename_rule",
    srcs = ["generic/no-generic-test-filename.yaml"],
)

semgrep_test(
    name = "no_generic_test_filename_test",
    srcs = ["//bazel/semgrep/tests:no_generic_test_filename_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_generic_test_filename_rule"],
    tags = ["semgrep"],
)

# no-unquoted-helm-range-args uses languages: [generic] so its fixture is a YAML
# file. This test wires the rule to its annotation fixture independently of
# yaml_rules_test (which scans yaml_fixtures, excluding this fixture).
filegroup(
    name = "no_unquoted_helm_range_args_rule",
    srcs = ["yaml/no-unquoted-helm-range-args.yaml"],
)

semgrep_test(
    name = "no_unquoted_helm_range_args_test",
    srcs = ["//bazel/semgrep/tests:no_unquoted_helm_range_args_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_unquoted_helm_range_args_rule"],
    tags = ["semgrep"],
)

# require-component-label-in-selector uses languages: [generic] with deployment path filters.
# This test wires the rule to its annotation fixture independently of
# kubernetes_rules_test (which uses yaml-language fixtures).
filegroup(
    name = "require_component_label_in_selector_rule",
    srcs = ["kubernetes/require-component-label-in-selector.yaml"],
)

semgrep_test(
    name = "require_component_label_in_selector_test",
    srcs = ["//bazel/semgrep/tests:require_component_label_in_selector_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":require_component_label_in_selector_rule"],
    tags = ["semgrep"],
)

# stale-copyright-year uses languages: [generic] so its fixture is a YAML file.
# This test wires the rule to its annotation fixture independently of
# golang_rules_test (which only scans *.go sources).
filegroup(
    name = "stale_copyright_year_rule",
    srcs = ["golang/stale-copyright-year.yaml"],
)

semgrep_test(
    name = "stale_copyright_year_test",
    srcs = ["//bazel/semgrep/tests:stale_copyright_year_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":stale_copyright_year_rule"],
    tags = ["semgrep"],
)

# no-deprecated-api-subdomain uses languages: [generic] scoped to projects/**/*.md.
# This test wires the rule to its colocated annotation fixture independently of
# generic_rules_test (which only scans no-stale-repo-paths fixtures).
filegroup(
    name = "no_deprecated_api_subdomain_rule",
    srcs = ["generic/no-deprecated-api-subdomain.yaml"],
)

semgrep_test(
    name = "no_deprecated_api_subdomain_test",
    srcs = ["generic/no-deprecated-api-subdomain.txt"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_deprecated_api_subdomain_rule"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "shell_rules_test",
    srcs = glob(["shell/*.bash"]),
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":shell_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "bazel_rules_test",
    srcs = glob(["bazel/*.build"]),
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":bazel_rules"],
    tags = ["semgrep"],
)

semgrep_test(
    name = "dockerfile_rules_test",
    srcs = glob(["dockerfile/*.dockerfile"]),
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":dockerfile_rules"],
    tags = ["semgrep"],
)

# no-create-extension-sql uses languages: [generic] so its fixture is a YAML file.
# This test wires the rule to its annotation fixture independently of other rule tests.
semgrep_test(
    name = "sql_rules_test",
    srcs = ["//bazel/semgrep/tests:no_create_extension_sql_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":sql_rules"],
    tags = ["semgrep"],
)

# migration-destructive-ddl uses languages: [generic] scoped to **/chart/migrations/*.sql.
# Wired to its own .sql annotation fixture independently of python_rules_test
# (which scans python/*.py sources). Excluded from python_rules because semgrep-core
# ignores paths: filters and would scan all Python files if included.
filegroup(
    name = "migration_destructive_ddl_rule",
    srcs = ["python/migration-destructive-ddl.yaml"],
)

semgrep_test(
    name = "migration_destructive_ddl_test",
    srcs = ["//bazel/semgrep/tests:migration_destructive_ddl_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":migration_destructive_ddl_rule"],
    tags = ["semgrep"],
)

# claude-print-missing-permission-mode uses languages: [generic] so its fixture is a YAML file.
# This test wires the rule to its annotation fixture independently of shell_rules_test
# (which scans shell/*.bash sources and does not include YAML-annotated generic fixtures).
filegroup(
    name = "claude_print_missing_permission_mode_rule",
    srcs = ["shell/claude-print-missing-permission-mode.yaml"],
)

semgrep_test(
    name = "claude_print_missing_permission_mode_test",
    srcs = ["//bazel/semgrep/tests:claude_print_missing_permission_mode_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":claude_print_missing_permission_mode_rule"],
    tags = ["semgrep"],
)

# no-session-in-to-thread uses languages: [python] so its fixture is a YAML file.
# This test wires the rule to its annotation fixture independently of python_rules_test
# (which scans python/*.py sources and does not include YAML-annotated fixtures).
filegroup(
    name = "no_session_in_to_thread_rule",
    srcs = ["python/no-session-in-to-thread.yaml"],
)

semgrep_test(
    name = "no_session_in_to_thread_test",
    srcs = ["//bazel/semgrep/tests:no_session_in_to_thread_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_session_in_to_thread_rule"],
    tags = ["semgrep"],
)

# no-bare-nosemgrep uses languages: [python] and is tested by python_rules_test via the
# colocated python/no-bare-nosemgrep.py fixture. This target also wires the rule to the
# tests/fixtures version for an independent dedicated test.
filegroup(
    name = "no_bare_nosemgrep_rule",
    srcs = ["python/no-bare-nosemgrep.yaml"],
)

semgrep_test(
    name = "no_bare_nosemgrep_test",
    srcs = ["//bazel/semgrep/tests:no_bare_nosemgrep_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_bare_nosemgrep_rule"],
    tags = ["semgrep"],
)

# sveltekit-public-route-fetches-private-knowledge-api uses languages: [generic] scoped to
# **/routes/public/**/*page.server.*. Wired to its own .js annotation fixture independently of
# typescript_rules_test (which only scans *.ts fixtures).
filegroup(
    name = "sveltekit_public_route_fetches_private_knowledge_api_rule",
    srcs = ["typescript/sveltekit-public-route-fetches-private-knowledge-api.yaml"],
)

semgrep_test(
    name = "sveltekit_public_route_fetches_private_knowledge_api_test",
    srcs = ["//bazel/semgrep/tests:sveltekit_public_route_fetches_private_knowledge_api_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":sveltekit_public_route_fetches_private_knowledge_api_rule"],
    tags = ["semgrep"],
)

# sveltekit-server-hardcoded-api-base-fallback uses languages: [generic] scoped to
# **/*+page.server.* and **/*+layout.server.*. Wired to its own .js annotation fixture
# independently of typescript_rules_test (which only scans *.ts fixtures).
filegroup(
    name = "sveltekit_server_hardcoded_api_base_fallback_rule",
    srcs = ["typescript/sveltekit-server-hardcoded-api-base-fallback.yaml"],
)

semgrep_test(
    name = "sveltekit_server_hardcoded_api_base_fallback_test",
    srcs = ["//bazel/semgrep/tests:sveltekit_server_hardcoded_api_base_fallback_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":sveltekit_server_hardcoded_api_base_fallback_rule"],
    tags = ["semgrep"],
)

# cfingress-rewrite-needs-slash-redirect uses languages: [generic] scoped to
# **/templates/httproute.yaml. Wired to its own annotation fixture independently
# of yaml_rules_test (which scans yaml_fixtures, excluding this fixture).
filegroup(
    name = "cfingress_rewrite_needs_slash_redirect_rule",
    srcs = ["yaml/cfingress-rewrite-needs-slash-redirect.yaml"],
)

semgrep_test(
    name = "cfingress_rewrite_needs_slash_redirect_test",
    srcs = ["//bazel/semgrep/tests:cfingress_rewrite_needs_slash_redirect_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":cfingress_rewrite_needs_slash_redirect_rule"],
    tags = ["semgrep"],
)

# css-import-after-rules uses languages: [generic] scoped to **/*.css.
# Wired to its own .css annotation fixture independently of generic_rules_test
# (which only scans no-stale-repo-paths fixtures).
filegroup(
    name = "css_import_after_rules_rule",
    srcs = ["generic/css-import-after-rules.yaml"],
)

semgrep_test(
    name = "css_import_after_rules_test",
    srcs = ["//bazel/semgrep/tests:css_import_after_rules_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":css_import_after_rules_rule"],
    tags = ["semgrep"],
)

# sveltekit-form-action-unvalidated-path uses languages: [javascript, typescript] to catch
# SSRF via unvalidated data.get() paths passed directly into fetch(). Wired to its own
# .js annotation fixture independently of typescript_rules_test (which scans *.ts fixtures).
filegroup(
    name = "sveltekit_form_action_unvalidated_path_rule",
    srcs = ["typescript/sveltekit-form-action-unvalidated-path.yaml"],
)

semgrep_test(
    name = "sveltekit_form_action_unvalidated_path_test",
    srcs = ["//bazel/semgrep/tests:sveltekit_form_action_unvalidated_path_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":sveltekit_form_action_unvalidated_path_rule"],
    tags = ["semgrep"],
)

# no-shallow-routing-for-url-state uses languages: [generic] scoped to projects sources.
# Wired to its own bad/ok .svelte annotation fixtures independently of
# typescript_rules_test (which only scans *.ts fixtures). The rule also runs for
# real against the frontend Svelte tree via
# //projects/monolith/frontend:svelte_url_state_semgrep_test.
filegroup(
    name = "no_shallow_routing_for_url_state_rule",
    srcs = ["typescript/no-shallow-routing-for-url-state.yaml"],
)

semgrep_test(
    name = "no_shallow_routing_for_url_state_test",
    srcs = ["//bazel/semgrep/tests:no_shallow_routing_for_url_state_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":no_shallow_routing_for_url_state_rule"],
    tags = ["semgrep"],
)

# svelte-hardcoded-color-in-style uses languages: [generic] scoped to **/*.svelte.
# Wired to its own .svelte annotation fixture independently of typescript_rules_test
# (which only scans *.ts fixtures).
filegroup(
    name = "svelte_hardcoded_color_in_style_rule",
    srcs = ["typescript/svelte-hardcoded-color-in-style.yaml"],
)

semgrep_test(
    name = "svelte_hardcoded_color_in_style_test",
    srcs = ["//bazel/semgrep/tests:svelte_hardcoded_color_in_style_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":svelte_hardcoded_color_in_style_rule"],
    tags = ["semgrep"],
)

# memory-request-ne-limit enforces requests==limits for memory in deploy and chart
# values files. Excluded from yaml_rules_test (which would run it against fixtures
# containing intentional request!=limit mismatches from other rules). Tested here
# against dedicated ok/bad fixture files in bazel/semgrep/tests/yaml/.
filegroup(
    name = "memory_request_ne_limit_rule",
    srcs = ["yaml/memory-request-ne-limit.yaml"],
)

semgrep_test(
    name = "memory_request_ne_limit_test",
    srcs = ["//bazel/semgrep/tests:memory_request_ne_limit_fixtures"],
    env = {"SEMGREP_TEST_MODE": "1"},
    rules = [":memory_request_ne_limit_rule"],
    tags = ["semgrep"],
)
