load("@aspect_rules_ts//ts:defs.bzl", "ts_project")

ts_project(
    name = "routes",
    srcs = [
        "DMLive.tsx",
        "DMPrep.tsx",
        "PlayerChar.tsx",
        "PlayerLive.tsx",
    ],
    no_emit = True,
    preserve_jsx = False,
    tsconfig = "//projects/grimoire/frontend:tsconfig",
    deps = [
        "//projects/grimoire/frontend:node_modules/@types/react",
        "//projects/grimoire/frontend:node_modules/react",
        "//projects/grimoire/frontend/src/components",
        "//projects/grimoire/frontend/src/lib",
        "//projects/grimoire/frontend/src/types",
    ],
)
