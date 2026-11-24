# The Cilium values, read by substrate/resources/resources_test.py: the egress
# scanner runs under gVisor and reaches the engine gate only while
# socketLB.hostNamespaceOnly stays true (substrate/egress-scan/gate/README.md).
exports_files(
    ["values.yaml"],
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)
