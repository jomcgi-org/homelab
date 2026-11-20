"""Provide a local executable archive for the extraction test."""

load("//bazel/tools/http:multiarch_http_file.bzl", "multiarch_http_file")

def _fixture_impl(ctx):
    multiarch_http_file(
        name = "http_archive_fixture",
        amd64_url = "file://" + str(ctx.path(Label("//bazel/tools/http/test:fixture.tar.bz2"))),
        amd64_sha256 = "8a0a56a23b93a66dad5086f80dc1a1b76d8e0ed19b01a063fb350c17bed9aca9",
        binary_name = "goose",
        extract_path = "goose",
    )

fixture = module_extension(implementation = _fixture_impl)
