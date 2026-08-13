# gazelle:exclude **

package(default_visibility = ["//visibility:public"])

filegroup(
    name = "yaml_fixtures",
    srcs = glob(
        ["fixtures/*.yaml"],
        # Exclude Go-rule reference fixtures that are not tested against yaml_rules.
        # Exclude SQL/generic and kubernetes-specific fixtures that are tested separately.
        exclude = [
            "fixtures/boto3-endpoint-url-missing-scheme.yaml",
            "fixtures/claude-print-missing-permission-mode.yaml",
            "fixtures/duckdb-bound-param-in-array-distance.yaml",
            "fixtures/fastapi-bare-tuple-return.yaml",
            "fixtures/logger-exc-info-non-boolean.yaml",
            "fixtures/no-create-extension-sql.yaml",
            "fixtures/no-deprecated-api-subdomain.yaml",
            "fixtures/no-discarded-json-marshal.yaml",
            "fixtures/no-session-in-to-thread.yaml",
            "fixtures/no-shared-preload-libraries-cnpg.yaml",
            "fixtures/cfingress-rewrite-needs-slash-redirect.yaml",
            "fixtures/no-unquoted-helm-range-args.yaml",
            "fixtures/python-shadow-module-import.yaml",
            "fixtures/require-cnpg-cluster-resources.yaml",
            "fixtures/require-component-label-in-selector.yaml",
            "fixtures/sqlmodel-select-missing-deleted-at-filter.yaml",
            "fixtures/unsafe-json-field-access.yaml",
        ],
    ),
)

# Fixture for the stale-copyright-year rule (languages: generic).
# Referenced by //bazel/semgrep/rules:stale_copyright_year_test.
filegroup(
    name = "stale_copyright_year_fixtures",
    srcs = ["fixtures/stale-copyright-year.yaml"],
)

filegroup(
    name = "typescript_fixtures",
    srcs = glob(["fixtures/*.ts"]),
)

# Fixture for the sveltekit-public-route-fetches-private-knowledge-api rule (languages: generic).
# Referenced by //bazel/semgrep/rules:sveltekit_public_route_fetches_private_knowledge_api_test.
filegroup(
    name = "sveltekit_public_route_fetches_private_knowledge_api_fixtures",
    srcs = ["fixtures/sveltekit-public-route-fetches-private-knowledge-api.js"],
)

# Fixture for the sveltekit-server-hardcoded-api-base-fallback rule (languages: generic).
# Referenced by //bazel/semgrep/rules:sveltekit_server_hardcoded_api_base_fallback_test.
filegroup(
    name = "sveltekit_server_hardcoded_api_base_fallback_fixtures",
    srcs = ["fixtures/sveltekit-server-hardcoded-api-base-fallback.js"],
)

# Fixture for the no-create-event-dispatcher-runes rule (languages: generic, paths: projects/**/*.svelte).
# Referenced by //bazel/semgrep/rules:no_create_event_dispatcher_runes_test.
filegroup(
    name = "no_create_event_dispatcher_runes_fixtures",
    srcs = ["fixtures/no-create-event-dispatcher-runes.svelte"],
)

# Fixtures for the no-shallow-routing-for-url-state rule (languages: generic).
# The rule matches across lines (import, then the searchParams read), so a single
# file cannot hold both a violating and a non-violating case: the spanning regex
# would join them. Split into bad/ok files like memory-request-ne-limit.
# Referenced by //bazel/semgrep/rules:no_shallow_routing_for_url_state_test.
filegroup(
    name = "no_shallow_routing_for_url_state_fixtures",
    srcs = glob(["svelte/no-shallow-routing-for-url-state/*.svelte"]),
)

# Fixture for the svelte-hardcoded-color-in-style rule (languages: generic, paths: **/*.svelte).
# Referenced by //bazel/semgrep/rules:svelte_hardcoded_color_in_style_test.
filegroup(
    name = "svelte_hardcoded_color_in_style_fixtures",
    srcs = ["fixtures/svelte-hardcoded-color-in-style.svelte"],
)

# Fixture for the no-stale-repo-paths rule (languages: generic).
# Referenced by //bazel/semgrep/rules:generic_rules_test.
filegroup(
    name = "no_stale_repo_paths_fixtures",
    srcs = ["fixtures/no-stale-repo-paths.md"],
)

# Fixture for the no-create-extension-sql rule (languages: generic, paths: **/migrations/*.sql).
# Referenced by //bazel/semgrep/rules:sql_rules_test.
filegroup(
    name = "no_create_extension_sql_fixtures",
    srcs = ["fixtures/no-create-extension-sql.yaml"],
)

# Fixture for the no-generic-test-filename rule (languages: generic, paths-filtered).
# Referenced by //bazel/semgrep/rules:no_generic_test_filename_test.
filegroup(
    name = "no_generic_test_filename_fixtures",
    srcs = ["fixtures/no-generic-test-filename.py"],
)

# Fixture for the no-unquoted-helm-range-args rule (languages: generic).
# Referenced by //bazel/semgrep/rules:no_unquoted_helm_range_args_test.
filegroup(
    name = "no_unquoted_helm_range_args_fixtures",
    srcs = ["fixtures/no-unquoted-helm-range-args.yaml"],
)

# Fixture for the require-component-label-in-selector rule (languages: generic).
# Referenced by //bazel/semgrep/rules:require_component_label_in_selector_test.
filegroup(
    name = "require_component_label_in_selector_fixtures",
    srcs = ["fixtures/require-component-label-in-selector.yaml"],
)

# Fixture for the claude-print-missing-permission-mode rule (languages: generic, paths: shell *.sh).
# Referenced by //bazel/semgrep/rules:claude_print_missing_permission_mode_test.
filegroup(
    name = "claude_print_missing_permission_mode_fixtures",
    srcs = ["fixtures/claude-print-missing-permission-mode.yaml"],
)

# Fixture for the no-session-in-to-thread rule (languages: python).
# Referenced by //bazel/semgrep/rules:no_session_in_to_thread_test.
filegroup(
    name = "no_session_in_to_thread_fixtures",
    srcs = ["fixtures/no-session-in-to-thread.yaml"],
)

# Fixture for the css-import-after-rules rule (languages: generic, paths: **/*.css).
# Referenced by //bazel/semgrep/rules:css_import_after_rules_test.
filegroup(
    name = "css_import_after_rules_fixtures",
    srcs = ["fixtures/css-import-after-rules.css"],
)

# Fixture for the sveltekit-form-action-unvalidated-path rule (languages: javascript, typescript).
# Referenced by //bazel/semgrep/rules:sveltekit_form_action_unvalidated_path_test.
filegroup(
    name = "sveltekit_form_action_unvalidated_path_fixtures",
    srcs = ["fixtures/sveltekit-form-action-unvalidated-path.js"],
)

# Fixtures for kubernetes rules (require-readiness-probe, require-resource-limits, etc.).
# Referenced by //bazel/semgrep/rules:kubernetes_rules_test.
# Fixture for the cfingress-rewrite-needs-slash-redirect rule (languages: generic,
# paths: **/templates/httproute.yaml). Excluded from yaml_fixtures because the rule
# uses a forward-scanning pattern-not that requires the ok and bad cases to be ordered
# carefully, and it targets template files rather than plain YAML.
# Referenced by //bazel/semgrep/rules:cfingress_rewrite_needs_slash_redirect_test.
filegroup(
    name = "cfingress_rewrite_needs_slash_redirect_fixtures",
    srcs = ["fixtures/cfingress-rewrite-needs-slash-redirect.yaml"],
)

# Fixture for the no-bare-nosemgrep rule (languages: python).
# Referenced by //bazel/semgrep/rules:no_bare_nosemgrep_test.
filegroup(
    name = "no_bare_nosemgrep_fixtures",
    srcs = ["fixtures/no-bare-nosemgrep.py"],
)

# Fixture for the migration-destructive-ddl rule (languages: generic, paths: **/chart/migrations/*.sql).
# Referenced by //bazel/semgrep/rules:migration_destructive_ddl_test.
filegroup(
    name = "migration_destructive_ddl_fixtures",
    srcs = ["fixtures/migration-destructive-ddl.sql"],
)

# New rule fixtures should be added here as they are created.
filegroup(
    name = "kubernetes_yaml_fixtures",
    srcs = glob([
        "fixtures/no-hardcoded-image-digest.yaml",
        "fixtures/no-host-network.yaml",
        "fixtures/no-privileged.yaml",
        "fixtures/no-shared-preload-libraries-cnpg.yaml",
        "fixtures/pdb-minAvailable-blocks-eviction.yaml",
        "fixtures/require-cnpg-cluster-resources.yaml",
        "fixtures/require-fsgroup-with-pvc.yaml",
        "fixtures/require-readiness-probe.yaml",
        "fixtures/require-resource-limits.yaml",
        "fixtures/require-tmp-emptydir.yaml",
    ]),
)

# Fixtures for the memory-request-ne-limit rule (ok and bad cases in separate files).
# Referenced by //bazel/semgrep/rules:memory_request_ne_limit_test.
filegroup(
    name = "memory_request_ne_limit_fixtures",
    srcs = glob(["yaml/memory-request-ne-limit/*.yaml"]),
)
