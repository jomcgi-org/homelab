# Cross-language node.proto round-trip test (Task 3 acceptance): the Elixir
# client generated from node.proto drives the Go fake NodeService server over the
# wire, exercising every RPC including the server-streaming WatchNode. Hand-
# managed (it wires the control app's mix build to a generated stub and a Go
# binary), so gazelle leaves this directory alone.
# gazelle:ignore

# Build the control app with the generated node.pb.ex + the round-trip test, plus
# the Go fake server, and run `mix test` on the RBE executor. no-remote-cache so
# it re-verifies against the live executor (matching mix_test_smoke). The general
# control test run (//bazel/erlang:mix_test_smoke) is untouched: it stages neither
# node.pb.ex nor the Go binary, and this test file lives outside control/test/.
genrule(
    name = "node_roundtrip_test",
    srcs = [
        "@otp_ubuntu2204_amd64//:otp",
        "@otp_ubuntu2204_amd64//:Install",
        "@elixir_1_18_4//:elixir",
        "@elixir_1_18_4//:bin/elixir",
        "//projects/embervm/control:srcs",
        "//projects/embervm/control:mix.exs",
        "@hex_archive//file",
        "//bazel/erlang:hex_tarballs",
        "//projects/embervm/proto/embervm/node/v1:node_elixir_src",
        "node_roundtrip_test.exs",
    ],
    outs = ["node_roundtrip_test.txt"],
    cmd = "$(location //bazel/erlang:roundtrip_test.sh) " +
          "\"$(location @otp_ubuntu2204_amd64//:Install)\" " +
          "\"$(location @elixir_1_18_4//:bin/elixir)\" " +
          "\"$(location //projects/embervm/control:mix.exs)\" " +
          "\"$@\" " +
          "\"$(location @hex_archive//file)\" " +
          "\"$(location //projects/embervm/proto/embervm/node/v1:node_elixir_src)\" " +
          "\"$(location node_roundtrip_test.exs)\" " +
          "\"$(location //projects/embervm/proto/embervm/node/v1/fakenode)\" " +
          "$(locations //bazel/erlang:hex_tarballs)",
    tags = ["no-remote-cache"],
    tools = [
        "//bazel/erlang:roundtrip_test.sh",
        "//projects/embervm/proto/embervm/node/v1/fakenode",
    ],
)
