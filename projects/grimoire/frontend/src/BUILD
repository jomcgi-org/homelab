load("@aspect_rules_ts//ts:defs.bzl", "ts_project")

ts_project(
    name = "src",
    srcs = [
        "App.tsx",
        "main.tsx",
        "vite-env.d.ts",
    ],
    no_emit = True,
    preserve_jsx = False,
    tsconfig = "//projects/grimoire/frontend:tsconfig",
    deps = [
        "//projects/grimoire/frontend:node_modules/@tanstack/react-query",
        "//projects/grimoire/frontend:node_modules/@types/react",
        "//projects/grimoire/frontend:node_modules/@types/react-dom",
        "//projects/grimoire/frontend:node_modules/react",
        "//projects/grimoire/frontend:node_modules/react-dom",
        "//projects/grimoire/frontend:node_modules/vite",
        "//projects/grimoire/frontend/src/components",
        "//projects/grimoire/frontend/src/hooks",
        "//projects/grimoire/frontend/src/lib",
        "//projects/grimoire/frontend/src/routes",
    ],
)
