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
