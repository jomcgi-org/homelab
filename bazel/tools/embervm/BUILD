load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

exports_files(["measure-vm-overhead.sh"])

sh_test(
    name = "measure_vm_overhead_test",
    srcs = ["measure-vm-overhead_test.sh"],
    data = [":measure-vm-overhead.sh"],
)
