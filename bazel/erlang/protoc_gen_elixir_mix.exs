defmodule ProtocGenElixir.MixProject do
  use Mix.Project

  # Minimal project whose only job is to build the protoc-gen-elixir escript out
  # of the protobuf hex package. protobuf's own mix.exs defines that escript
  # (main_module Protobuf.Protoc.CLI, name protoc-gen-elixir); we re-declare it
  # here so `mix escript.build` in THIS project produces it while pulling protobuf
  # as a `path:` dep (staged from the pinned hex tarball by gen_elixir.sh, so mix
  # never contacts hex.pm). Since protobuf 0.17 the gRPC Service/Stub generator
  # lives inside protobuf itself, so protobuf ALONE is the codegen closure; its
  # only dep, jason, is optional and unused for binary-proto codegen, so it is
  # not staged.
  def project do
    [
      app: :protoc_gen_elixir,
      version: "0.1.0",
      elixir: "~> 1.18",
      deps: deps(),
      escript: [main_module: Protobuf.Protoc.CLI, name: "protoc-gen-elixir"]
    ]
  end

  def application, do: [extra_applications: [:logger]]

  defp deps do
    [
      {:protobuf, path: "deps/protobuf"}
    ]
  end
end
