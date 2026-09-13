"""Module extension for the Envoy serving-edge runtime test binary."""

load(":oci_envoy.bzl", "oci_envoy")

def _envoy_impl(_module_ctx):
    oci_envoy(
        name = "envoy_test_binary",
        image = "@envoy_test_linux_amd64//:index.json",
    )

envoy = module_extension(implementation = _envoy_impl)
