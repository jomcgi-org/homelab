# The files ../../../resources renders through its "../egress-scan/gate/guard"
# resource, and ../manifests renders through "../guard".
filegroup(
    name = "kustomize_inputs",
    srcs = glob(["*.yaml"]),
    visibility = [
        "//projects/platform-factory/substrate/egress-scan/gate/manifests:__pkg__",
        "//projects/platform-factory/substrate/resources:__pkg__",
    ],
)
