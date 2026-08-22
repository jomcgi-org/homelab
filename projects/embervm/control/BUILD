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
        # Generated node.proto Elixir stub, staged into control/lib/ by
        # mix_release.sh so Embervm.NodeRegistry (Task 9) enters the release.
        # Promoting the gRPC deps to prod (mix.exs) + baking this stub is why the
        # release image digest changes and this PR bumps the chart.
        "//projects/embervm/proto/embervm/node/v1:node_elixir_src",
        "//bazel/erlang:hex_tarballs",
        "@rebar3//file",
    ],
    outs = ["embervm-release.tar"],
    cmd = "MIX_REBAR3_SRC=\"$$(pwd)/$(location @rebar3//file)\" $(location //bazel/erlang:mix_release.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location @elixir_1_18_4//:bin/elixir)\" \"$(location mix.exs)\" \"$@\" \"$(location @hex_archive//file)\" \"$(location //projects/embervm/proto/embervm/node/v1:node_elixir_src)\" $(locations //bazel/erlang:hex_tarballs)",
    tools = ["//bazel/erlang:mix_release.sh"],
    visibility = ["//projects/embervm:__subpackages__"],
)

# Proves the built release BOOTS and serves /healthz on the executor using
# external erlang exactly like the deployed pod (include_erts:false,
# RELEASE_DISTRIBUTION=none, RELEASE_TMP, OTP matching the image's Wolfi
# erlang-27). The last runtime unknown after compile/ExUnit/release-build; also a
# permanent boot regression guard. Colocated with :release_tar (same package) so
# no cross-package visibility is needed.
genrule(
    name = "release_boot_smoke",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
        ":release_tar",
    ],
    outs = ["release_boot_smoke.txt"],
    cmd = "$(location //bazel/erlang:release_boot.sh) \"$(location @otp_ubuntu2204_amd64//:Install)\" \"$(location :release_tar)\" \"$@\"",
        tools = ["//bazel/erlang:release_boot.sh"],
)
