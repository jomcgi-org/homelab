# gazelle:ignore
load("@rules_go//go:def.bzl", "go_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

go_binary(
    name = "kubectl",
    srcs = ["main.go"],
    gc_linkopts = [
        "-s",
        "-w",
    ],
    goarch = "wasm",
    goos = "js",
    pure = "on",
    x_defs = {"k8s.io/component-base/version.gitVersion": "v1.37.2"},
    deps = [
        "@wasm_cli//pkg/genericclioptions",
        "@wasm_cli//pkg/genericiooptions",
        "@wasm_client//rest",
        "@wasm_cobra//:cobra",
        "@wasm_kubectl//pkg/cmd/apiresources",
        "@wasm_kubectl//pkg/cmd/auth",
        "@wasm_kubectl//pkg/cmd/describe",
        "@wasm_kubectl//pkg/cmd/events",
        "@wasm_kubectl//pkg/cmd/explain",
        "@wasm_kubectl//pkg/cmd/get",
        "@wasm_kubectl//pkg/cmd/top",
        "@wasm_kubectl//pkg/cmd/util",
        "@wasm_kubectl//pkg/cmd/version",
    ],
)

genrule(
    name = "kubectl-wasm",
    srcs = [":kubectl"],
    outs = ["kubectl.wasm.gz"],
    cmd = "gzip -9nc $< > $@ && test $$(wc -c < $@) -lt 26214400",
    visibility = ["//visibility:public"],
)

sh_test(
    name = "browser_test",
    srcs = ["browser_test.sh"],
    args = [
        "$(rootpath @nodejs_linux_amd64//:node_bin)",
        "$(rootpath :browser_test.cjs)",
        "$(rootpath @wasm_go_sdk//:lib/wasm/wasm_exec.js)",
        "$(rootpath :kubectl-wasm)",
    ],
    data = [
        "browser_test.cjs",
        ":kubectl-wasm",
        "@nodejs_linux_amd64//:node_bin",
        "@wasm_go_sdk//:lib/wasm/wasm_exec.js",
    ],
)
