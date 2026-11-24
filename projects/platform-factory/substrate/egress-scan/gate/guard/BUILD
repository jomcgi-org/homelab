# The files of the admission guard (not wired since 2026-11-24). ../manifests
# renders them through "../guard".
filegroup(
    name = "kustomize_inputs",
    srcs = glob(["*.yaml"]),
    visibility = [
        "//projects/platform-factory/substrate/egress-scan/gate/manifests:__pkg__",
        "//projects/platform-factory/substrate/resources:__pkg__",
    ],
)
