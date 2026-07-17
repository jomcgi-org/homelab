load("@aspect_rules_ts//ts:defs.bzl", "ts_project")

ts_project(
    name = "lib",
    srcs = [
        "api.ts",
        "mock-data.ts",
        "store.ts",
        "tokens.ts",
        "ws.ts",
    ],
    no_emit = True,
    preserve_jsx = False,
    tsconfig = "//projects/grimoire/frontend:tsconfig",
    deps = [
        "//projects/grimoire/frontend:node_modules/@tanstack/react-query",
        "//projects/grimoire/frontend:node_modules/zustand",
        "//projects/grimoire/frontend/src/types",
    ],
)
