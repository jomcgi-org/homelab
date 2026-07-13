# node.proto is the EmberVM control-to-node gRPC contract (the design seam).
# Proto codegen infrastructure (rules_proto + Go/Elixir stubs) is a later,
# decision-gated bootstrap; until it lands the .proto is a contract artifact
# only. Disable gazelle proto-rule generation for this package so it does not
# synthesize a proto_library loading @rules_proto (absent from MODULE.bazel).
# The repo pins gazelle to the `BUILD` filename, so this directive must live in
# a file named BUILD, co-located with the .proto.
# gazelle:proto disable
