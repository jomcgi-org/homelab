# This tool imports itself as the top-level `bench` package (for example
# `from bench.sandbox import run_sandboxed`), and it lives in a subdirectory whose
# name (`model-bench`) is not a valid Python module path. Mark this directory as the
# Python import root so gazelle resolves first-party `bench.*` imports to targets
# under `//projects/model-bench/bench/...` instead of looking for `bench/` at the
# repo root. Without this, gazelle fails dependency validation for every intra-package
# import. gazelle generates the py_library / py_test targets below this line.
# gazelle:python_root
#
# bench/__main__.py is the `python -m bench` shim (vendored python only, not a Bazel
# target). It is excluded because a guarded/entrypoint module would make gazelle emit a
# py_binary, which this repo maps to py_venv_binary (mandatory `main`) and fails to load.
# gazelle:exclude bench/__main__.py
#
# tasks/ holds benchmark fixtures (including intentionally imperfect code such as an
# RBAC handler.py that a task asks the model to fix). Exclude it so gazelle does not
# turn fixtures into build targets or semgrep-scan deliberately flawed fixture code.
# gazelle:exclude tasks

# Hand-authored test data; Gazelle still generates the Python test targets.
filegroup(
    name = "stars_climatology_perf_data",
    srcs = [
        "tasks/stars-climatology-perf-01/reference/algorithmic.py",
        "tasks/stars-climatology-perf-01/reference/micro.py",
        "tasks/stars-climatology-perf-01/task.yaml",
    ],
    visibility = ["//projects/model-bench/bench:__pkg__"],
)

filegroup(
    name = "campsites_rollup_perf_data",
    srcs = [
        "tasks/campsites-region-rollup-perf-01/reference/algorithmic.py",
        "tasks/campsites-region-rollup-perf-01/reference/micro.py",
        "tasks/campsites-region-rollup-perf-01/task.yaml",
    ],
    visibility = ["//projects/model-bench/bench:__pkg__"],
)

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "task_specs",
    srcs = [
        "tasks/authz-race-01/task.yaml",
        "tasks/campsites-region-rollup-01/task.yaml",
        "tasks/campsites-region-rollup-perf-01/task.yaml",
        "tasks/chart-version-conflict-01/task.yaml",
        "tasks/chat-public-retention-review-01/task.yaml",
        "tasks/chat-public-retention-review-02/task.yaml",
        "tasks/chunker-mutation-01/task.yaml",
        "tasks/commit-message-01/task.yaml",
        "tasks/docs-public-markers-01/task.yaml",
        "tasks/embervm-capability-generation-01/task.yaml",
        "tasks/embervm-pi-tools-01/task.yaml",
        "tasks/factory-rollout-fence-01/task.yaml",
        "tasks/factory-spend-cache-01/task.yaml",
        "tasks/fc-warmpool-daemonset-01/task.yaml",
        "tasks/flights-module-01/task.yaml",
        "tasks/frontend-moving-legtags-01/task.yaml",
        "tasks/go-vsock-frame-01/task.yaml",
        "tasks/header-trust-6036-01/task.yaml",
        "tasks/helm-values-plumbing-01/task.yaml",
        "tasks/hikes-walkhighlands-dom-01/task.yaml",
        "tasks/hikes-walkhighlands-duration-01/task.yaml",
        "tasks/idor-multi-endpoint-01/task.yaml",
        "tasks/inference-appledouble-01/task.yaml",
        "tasks/null-content-fix-01/task.yaml",
        "tasks/rbac-endpoint-verbs-01/task.yaml",
        "tasks/research-adr-writeback-01/task.yaml",
        "tasks/rollout-handoff-logs-01/task.yaml",
        "tasks/rollout-http-drain-logs-01/task.yaml",
        "tasks/secret-inline-conflict-01/task.yaml",
        "tasks/security-review-01/task.yaml",
        "tasks/slo-budget-breach-01/task.yaml",
        "tasks/stars-climatology-months-01/task.yaml",
        "tasks/stars-climatology-perf-01/task.yaml",
        "tasks/stars-grid-speedup-01/task.yaml",
        "tasks/tokenbroker-spiffe-wiring-01/task.yaml",
        "tasks/trace-correlation-multisite-01/task.yaml",
        "tasks/whatsapp-timeparse-mutation-01/task.yaml",
        "tasks/worldcup-fixtures-guard-01/task.yaml",
        "tasks/worldcup-swing-settled-01/task.yaml",
    ],
    visibility = ["//projects/model-bench:__subpackages__"],
)
