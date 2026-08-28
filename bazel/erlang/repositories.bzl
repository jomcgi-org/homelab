"""Prebuilt Erlang/OTP for the EmberVM control-plane build.

Unlike bazel/ocaml (which builds its compiler from source because a prebuilt
linked a too-new glibc), the RBE executor here was probed to be Ubuntu 22.04.5
(glibc 2.35). hex.pm publishes OTP builds compiled *for* ubuntu-22.04, so the
matching prebuilt runs natively on the executor and its crypto app links the
executor's already-present runtime libssl.so.3 (pulled in by git/curl) with no
dev headers needed. This sidesteps the from-source OTP build + OpenSSL/ncurses
provisioning entirely.

The prebuilt is BUILD-time only: it runs mix/elixir to compile the control-plane
app to architecture-independent .beam bytecode. The release is built with
include_erts: false, and the apko runtime image supplies erlang-27 per-arch from
Wolfi, so the deployed control plane never uses this build-host OTP.

Elixir itself is architecture-independent .beam and is fetched separately (its
precompiled release only needs a working erl to run).
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

# Hex dependency tarballs, fetched hermetically at repo-fetch time (the host has
# network; the RBE executor does not). Hex tarballs are a nested format (an outer
# tar holding contents.tar.gz), which http_archive cannot unpack, so each is an
# http_file and bazel/erlang/mix_test.sh|mix_release.sh unpack the inner archive
# into the project's deps/ tree. The control-plane mix.exs then consumes them as
# `path:` deps (Path SCM), so mix never contacts hex.pm: no mix.lock, no .hex
# markers, no `mix deps.get`, no Hex archive install. This is the hex-dependency
# analog of the prebuilt-OTP de-risk.
#
# The closure below is two dependency groups, both fetched the same way:
#
#   1. exqlite (the SQLite op-log driver) plus everything it declares
#      non-optionally: db_connection -> telemetry, and the build-time
#      elixir_make + cc_precompiler. (table is exqlite's only optional dep and
#      is not pulled.)
#   2. The HTTP + K8s-client closure for the submit API (Task 8) and the
#      Workload watcher (Task 5): Bandit (HTTP server) + Plug (router) replace
#      the raw :gen_tcp health endpoint; Finch + Mint make the K8s API calls
#      (TokenReview POST, CRD watch stream). Their full non-optional transitive
#      closure, resolved from each package's hex requirements: bandit -> plug,
#      hpax, thousand_island, websock, telemetry; plug -> mime, plug_crypto,
#      telemetry; finch -> mint, nimble_options, nimble_pool, mime, telemetry;
#      mint -> hpax (castore is mint's ONLY dep and is OPTIONAL: it ships a
#      Mozilla CA bundle for TLS to arbitrary hosts, but every call we make is
#      to the in-cluster K8s API whose CA is the pod's own ca.crt passed via
#      transport_opts, so Mint never reaches for it). telemetry (1.4.2) and mime
#      are shared across both groups; hpax is shared by bandit and mint. All of
#      group 2 is pure Elixir/Erlang (no NIF, no rebar3), so the mix-built path
#      needs no new build tooling.
#
# sha256 is the outer tarball hash (verified with `shasum -a 256
# <name>-<version>.tar` against repo.hex.pm). When bumping a version, re-fetch
# the tarball and re-pin the sha here.
_HEX_DEPS = [
    ("exqlite", "0.38.0", "f3da7b6e7b08bd548c33a118890d0eb8c5395fe093b31c8b329663234d0e988e"),
    # Embervm.OpLog.Postgres (PR-4, #18/#27): postgrex reuses the already-vendored
    # db_connection (its `~> 2.1` bound is satisfied by 2.10.2 above) and adds one
    # new leaf, decimal (postgrex's `~> 1.5 or ~> 2.0` bound). postgrex's other
    # optional deps (jason, already vendored; table, an IEx result-formatting
    # helper this control plane never calls) are not pulled, mirroring how
    # exqlite's optional `table` dep is skipped above.
    ("postgrex", "0.20.0", "d36ef8b36f323d29505314f704e21a1a038e2dc387c6409ee0cd24144e187c0f"),
    ("decimal", "2.3.0", "a4d66355cb29cb47c3cf30e71329e58361cfcb37c34235ef3bf1d7bf3773aeac"),
    ("db_connection", "2.10.2", "510b14482330f1af6490a2fa0efd8d4f1435d1529b165647df22ac0f2df0fa93"),
    ("elixir_make", "0.9.0", "db23d4fd8b757462ad02f8aa73431a426fe6671c80b200d9710caf3d1dd0ffdb"),
    ("cc_precompiler", "0.1.11", "3427232caf0835f94680e5bcf082408a70b48ad68a5f5c0b02a3bea9f3a075b9"),
    ("telemetry", "1.4.2", "928f6495066506077862c0d1646609eed891a4326bee3126ba54b60af61febb1"),
    ("bandit", "1.12.0", "45dac82dc86f45cf4a196dee9cc5a8b791d9c9469d996055f055e6ee36c66e20"),
    ("finch", "0.23.0", "80e58d3f936f57e3fdf404f83a3642897ae6d9fb642934e46da4d8fe761b99d5"),
    ("hpax", "1.0.4", "afc7cb142ebcc2d01ce7816190b98ce5dd49e799111b24249f3443d730f377ca"),
    ("mime", "2.0.7", "6171188e399ee16023ffc5b76ce445eb6d9672e2e241d2df6050f3c771e80ccd"),
    ("mint", "1.9.1", "831101bd560b086316fab5f7adb21a4f3455717d8e4bc8368b052e09aa9163e0"),
    ("nimble_options", "1.1.1", "821b2470ca9442c4b6984882fe9bb0389371b8ddec4d45a9504f00a66f650b44"),
    ("nimble_pool", "1.1.0", "af2e4e6b34197db81f7aad230c1118eac993acc0dae6bc83bac0126d4ae0813a"),
    ("plug", "1.20.3", "be266aee1b8536ef6409d58cf39a3121319f0ec47cfa1b24024485aa0e76ad76"),
    ("plug_crypto", "2.1.1", "6470bce6ffe41c8bd497612ffde1a7e4af67f36a15eea5f921af71cf3e11247c"),
    ("thousand_island", "1.5.0", "708923d40523e43cf99041ab37a0d4b0ec426ac6438fa3716ab23d919eaeb412"),
    ("websock", "0.5.3", "6105453d7fac22c712ad66fab1d45abdf049868f253cf719b625151460b8b453"),
    #   3. The gRPC + protobuf closure for the node.proto client (Task 3). The
    #      protobuf package doubles as the codegen plugin: its escript
    #      (protoc-gen-elixir, main_module Protobuf.Protoc.CLI) is what protoc runs
    #      to emit .pb.ex, and generation of the gRPC Service/Stub modules moved
    #      into protobuf itself (generator/service.ex), so the escript build needs
    #      protobuf ALONE (jason is its only, optional, dep). At RUNTIME the
    #      generated stub does `use GRPC.Service`/`use GRPC.Stub`, so the control
    #      app additionally links grpc -> grpc_core -> {googleapis, jason,
    #      protobuf, telemetry}. grpc's client transport is the already-pinned Mint
    #      adapter (gun is grpc's OTHER, optional, transport and is not pulled), so
    #      no gun/cowlib/cowboy server subtree enters; the whole group is pure
    #      Elixir/Erlang (no NIF). telemetry (1.4.2), mint (1.9.1), and hpax are
    #      shared with group 2. googleapis is a hard dep of grpc_core (a bundle of
    #      precompiled google.* protos); its `~> 0.1.0` bound pins 0.1.0 exactly.
    #
    #      grpc is held at >= 1.0.3 for issue #4144. 1.0.2's Mint adapter has no
    #      `process_response/2` clause for `{:error, ref, reason}`, so a server
    #      cancelled stream (Mint reports `{:server_closed_request, :cancel}`)
    #      raised FunctionClauseError and killed the connection process. The
    #      status the node had already sent was discarded, and every following
    #      call on that dead pid failed with `:noproc`, so roughly two wake
    #      failures in three reported a dead channel instead of the node's real
    #      FAILED_PRECONDITION. 1.0.3 adds that clause and ends the stream
    #      cleanly. grpc 1.0.3 requires `grpc_core ~> 1.0.3`, so the pair moves
    #      together; grpc_core 1.0.3 needs protobuf `~> 0.17`, telemetry `~> 1.0`,
    #      googleapis `~> 0.1.0` and jason, all already satisfied below, so no
    #      new package enters the closure.
    ("grpc", "1.0.3", "fc80371b72001c56d5dd7bd24859b83d25d8960f1e67680712c98524bf4bb3a8"),
    ("grpc_core", "1.0.3", "8167fa6e06190d229df25b2386173b385add6682f87c27960699439083145f78"),
    ("googleapis", "0.1.0", "1989a7244fd17d3eb5f3de311a022b656c3736b39740db46506157c4604bd212"),
    ("jason", "1.4.5", "b0c823996102bcd0239b3c2444eb00409b72f6a140c1950bc8b457d836b30684"),
    ("protobuf", "0.17.0", "ca6c91f6f63e2c147b47f03eefd10b80538aa6fc55ff4b12b795efb786b0152f"),
    #   4. OpenTelemetry tracing (Task 13) with OTLP/gRPC export. The API +
    #      SDK (opentelemetry, opentelemetry_api, semantic_conventions) plus the
    #      OTLP exporter, whose transport is grpcbox -> {acceptor_pool, ctx, gproc,
    #      ts_chatterbox -> hpack_erl} with TLS cert checking via
    #      tls_certificate_check -> ssl_verify_fun. All pure Erlang/Elixir (no NIF),
    #      so the amd64-only image is unaffected. Resolved from hex (latest stable,
    #      co-released and mutually compatible). Package name != app name for
    #      hpack_erl (app: hpack) and ts_chatterbox (app: chatterbox); mix.exs maps
    #      those atoms to their deps/<package> path.
    ("opentelemetry_api", "1.5.0", "f53ec8a1337ae4a487d43ac89da4bd3a3c99ddf576655d071deed8b56a2d5dda"),
    ("opentelemetry", "1.7.0", "a9173b058c4549bf824cbc2f1d2fa2adc5cdedc22aa3f0f826951187bbd53131"),
    ("opentelemetry_exporter", "1.10.0", "33a116ed7304cb91783f779dec02478f887c87988077bfd72840f760b8d4b952"),
    ("opentelemetry_semantic_conventions", "1.27.0", "9681ccaa24fd3d810b4461581717661fd85ff7019b082c2dff89c7d5b1fc2864"),
    ("grpcbox", "0.18.0", "5ec9f8fe664ab51201b32c117a61511a1f9d6316771e3891ba8a88d289a732ab"),
    ("acceptor_pool", "1.0.1", "f172f3d74513e8edd445c257d596fc84dbdd56d2c6fa287434269648ae5a421e"),
    ("ctx", "0.6.0", "a14ed2d1b67723dbebbe423b28d7615eb0bdcba6ff28f2d1f1b0a7e1d4aa5fc2"),
    ("gproc", "1.2.0", "70c6f8c91fa5974296cd87974949d8eab953230414f31c4a623ff75131e0827a"),
    ("ts_chatterbox", "0.16.0", "34c145c702f3a8d22f49a189eb34579ef3db68f9a98a82d19b5cf6e390aad54f"),
    ("hpack_erl", "0.3.0", "d6137d7079169d8c485c6962dfe261af5b9ef60fbc557344511c1e65e3d95fb0"),
    ("tls_certificate_check", "1.33.0", "cab9a7439e2dbfe91b38104f2d8a4b6d61dbc4d3a5ad59ac364713a88c6cfd9b"),
    ("ssl_verify_fun", "1.1.7", "fe4c190e8f37401d30167c8c405eda19469f34577987c76dde613e838bbc67f8"),
]

# hex.pm's OTP build extracts to OTP-<ver>/ with erts-*/, lib/, and an Install
# script that bakes ERL_ROOT paths (run `Install -minimal <abs-root>` before use).
_OTP_BUILD = """
filegroup(
    name = "otp",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

exports_files(["Install"], visibility = ["//visibility:public"])
"""

# Elixir ships a precompiled release (bin/ + lib/, .beam bytecode) that is
# architecture-independent and only needs a working erl to run. So one Elixir
# archive serves every arch; it is fetched, not built.
_ELIXIR_BUILD = """
filegroup(
    name = "elixir",
    srcs = glob(["**"], exclude = ["BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel"]),
    visibility = ["//visibility:public"],
)

# bin/elixir anchors the Elixir root for staging (its dir's parent is the root).
exports_files(["bin/elixir"], visibility = ["//visibility:public"])
"""

# Prebuilt protoc for node.proto codegen (Task 3). Same de-risk as the OTP
# prebuilt: the release zip is fetched at repo-fetch time (host has network) and
# the amd64 binary runs natively on the amd64-only RBE executor, so no protobuf
# compiler is built from source. Single-arch by design: codegen is a build-host
# operation whose .pb.go/.pb.ex outputs are architecture-independent. bin/protoc
# is the compiler; include/ carries the well-known-type .protos protoc resolves
# for imports (node.proto imports none today, but keep them for future protos).
_PROTOC_BUILD = """
filegroup(
    name = "protoc",
    srcs = ["bin/protoc"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "well_known_protos",
    srcs = glob(["include/**/*.proto"]),
    visibility = ["//visibility:public"],
)

exports_files(["bin/protoc"], visibility = ["//visibility:public"])
"""

def _erlang_impl(_ctx):
    http_archive(
        # OTP version MUST match the apko image's Wolfi erlang-27 (27.3.4.16)
        # exactly: an include_erts:false release pins exact OTP app versions in
        # its .boot, so a build/runtime patch mismatch fails the pod at boot. If
        # Wolfi's erlang-27 bumps, re-pin both this and the image package together.
        #
        # The pin is only re-pinnable while BOTH sides still publish the version.
        # 27.3.4.2 was dropped from the Wolfi APKINDEX (its apk still resolves on
        # the CDN, which is why nothing broke), so `apko lock` could no longer
        # satisfy the image's `erlang-27=27.3.4.2-r0` constraint and every lock
        # refresh failed. Choose a version present in BOTH the Wolfi index and
        # builds.hex.pm/builds/otp/ubuntu-22.04, not merely the newest of one.
        name = "otp_ubuntu2204_amd64",
        urls = ["https://builds.hex.pm/builds/otp/ubuntu-22.04/OTP-27.3.4.16.tar.gz"],
        sha256 = "16b455351679b5e200594a5f6b742fd0f9f8d42b0dd2b35eebbbcd2288bb44bd",
        strip_prefix = "OTP-27.3.4.16",
        build_file_content = _OTP_BUILD,
    )
    http_archive(
        name = "elixir_1_18_4",
        urls = ["https://github.com/elixir-lang/elixir/releases/download/v1.18.4/elixir-otp-27.zip"],
        sha256 = "5be18f35e329f7c5914a80dd9f323d7bbb144616df1ed16f6f0862a1900b4bb5",
        build_file_content = _ELIXIR_BUILD,
    )
    http_archive(
        name = "protoc_linux_x86_64",
        urls = ["https://github.com/protocolbuffers/protobuf/releases/download/v35.1/protoc-35.1-linux-x86_64.zip"],
        sha256 = "6930ebf62bd4ea607b98fff052596c6ee564b9835b4ce172c75a3f53ae9d91b7",
        build_file_content = _PROTOC_BUILD,
    )
    for name, version, sha in _HEX_DEPS:
        http_file(
            name = "hex_%s" % name,
            urls = ["https://repo.hex.pm/tarballs/%s-%s.tar" % (name, version)],
            sha256 = sha,
            # Keep the <name>-<version>.tar filename: the staging scripts derive
            # the deps/<name>/ directory from it.
            downloaded_file_path = "%s-%s.tar" % (name, version),
        )

    # The Hex package manager archive. Elixir's precompiled release does not bundle
    # Hex, but mix needs the Hex SCM *registered* to even parse the hex-style dep
    # declarations inside our path deps' own mix.exs files (e.g. exqlite declaring
    # `{:db_connection, "~> 2.1"}`); without it mix aborts with "Could not find an
    # SCM for dependency". The mix drivers `mix archive.install` this offline so no
    # actual hex.pm fetch ever happens (the path overrides keep resolution local).
    # This is the exact build mix itself installs for Elixir 1.18 (installs/1.18.0/
    # hex.ez is an alias to the current Hex for that series); the pinned sha256
    # makes a silent alias rotation fail the cold fetch loudly instead of drifting.
    http_file(
        name = "hex_archive",
        urls = ["https://builds.hex.pm/installs/1.18.0/hex.ez"],
        sha256 = "55ea0adcd1adf5d26db47fcc69b365af98cd8afc06c78434c29db73b45758a28",
        downloaded_file_path = "hex.ez",
    )

    # rebar3 (Task 13): mix builds rebar-project deps (the OpenTelemetry gRPC
    # exporter's Erlang chain: gproc, grpcbox, ts_chatterbox, ...) by shelling out
    # to rebar3, which the prior all-mix closure never needed. The release asset is
    # a self-contained, arch-independent escript; the mix drivers stage it onto the
    # OTP bin (already on PATH) so mix finds it. Offline + SHA-pinned.
    http_file(
        name = "rebar3",
        urls = ["https://github.com/erlang/rebar3/releases/download/3.24.0/rebar3"],
        sha256 = "d2d31cfb98904b8e4917300a75f870de12cb5167cd6214d1043e973a56668a54",
        downloaded_file_path = "rebar3",
        executable = True,
    )

erlang = module_extension(
    implementation = _erlang_impl,
    doc = "Fetches the prebuilt ubuntu-22.04 OTP 27 (@otp_ubuntu2204_amd64), precompiled Elixir 1.18.4 (@elixir_1_18_4), the control-plane hex dependency tarballs (@hex_*), and the prebuilt protoc (@protoc_linux_x86_64) for node.proto codegen.",
)
