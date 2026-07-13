# EmberVM control-plane Elixir project. Building/testing it runs through the
# prebuilt OTP + Elixir toolchain in //bazel/erlang (see bazel/erlang/mix_test.sh
# and repositories.bzl). No gazelle-managed targets here (Elixir is not a gazelle
# language); the filegroup is hand-maintained.
filegroup(
    name = "srcs",
    srcs = glob(
        [
            "mix.exs",
            "lib/**",
            "test/**",
            "config/**",
        ],
        allow_empty = True,
    ),
    visibility = ["//visibility:public"],
)

exports_files(["mix.exs"])

# The control-plane OTP release, built by the prebuilt OTP + Elixir toolchain and
# emitted as one deterministic tar rooted at opt/embervm/ (arch-independent .beam;
# include_erts:false). Layered into the dual-arch apko image, which supplies
# erlang at runtime. See bazel/erlang/mix_release.sh.
genrule(
    name = "release_tar",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
        "@elixir_1_18_4//:elixir",
        "@elixir_1_18_4//:bin/elixir",
        ":srcs",
        "mix.exs",
        "@hex_archive//file",
        "//bazel/erlang:hex_tarballs",
    ],
    outs = ["embervm-release.tar"],
    cmd = "$(location //bazel/erlang:mix_release.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location @elixir_1_18_4//:bin/elixir)\" \"$(location mix.exs)\" \"$@\" \"$(location @hex_archive//file)\" $(locations //bazel/erlang:hex_tarballs)",
    tools = ["//bazel/erlang:mix_release.sh"],
    visibility = ["//projects/embervm:__subpackages__"],
)
