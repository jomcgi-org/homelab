# EmberVM protocol TLA+ specs, checked by TLC in CI (ADR embervm/006).
#
# Two specs, seven genrules run the //bazel/tla:tlc.sh driver:
#   adoption.tla (protocol 1: VM lifecycle + adoption) with four cfgs: rich
#     safety, positive liveness, and two NEGATIVE modes (expectation "fail") that
#     prove the model still reproduces a historical bug.
#   bank_relight.tla (protocol 2: session bank/relight generation pairing, added by
#     the ADR embervm/014 PR 5 follow-through) with three cfgs: a shared-lane safety
#     config, an isolated single-use safety config, and one NEGATIVE mode proving
#     the model still catches the pre-PR-#3770 generation regression.
# The driver fails the build if a "fail" mode finds NO violation (the model went
# blind). Safety and liveness are split across cfgs because exhaustive liveness over
# the rich safety graph is too slow for CI; see README.md for the cfg matrix.
#
# The JRE is staged as its whole :jre filegroup (bin/java needs its lib/ siblings)
# plus the :bin/java anchor, mirroring how //projects/embervm/control stages
# @otp's :otp filegroup + :Install anchor. tla2tools.jar comes from @tla2tools.
#
# No no-remote-cache tag: the check is deterministic in (spec, cfg, toolchain),
# so a cache hit is a correct skip and a full TLC re-run only happens when one of
# those changes. That is exactly the ADR's cost model (full checking on spec
# change, not every push).

# The staged JRE + jar, shared by every TLC genrule. The spec .tla each genrule
# checks is added per-target (adoption.tla or bank_relight.tla), so this list
# carries only the toolchain, not a spec.
_JRE_SRCS = [
    "@temurin21_jre_linux_amd64//:jre",
    "@temurin21_jre_linux_amd64//:bin/java",
    "@tla2tools//file",
]

_JAVA = "@temurin21_jre_linux_amd64//:bin/java"

_JAR = "@tla2tools//file"

genrule(
    name = "tlc_adoption",
    srcs = _JRE_SRCS + [
        "adoption.tla",
        "adoption.cfg",
    ],
    outs = ["adoption.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption.tla)\" " +
          "\"$(location adoption.cfg)\" " +
          "\"$@\" pass",
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_adoption_liveness",
    srcs = _JRE_SRCS + [
        "adoption.tla",
        "adoption_liveness.cfg",
    ],
    outs = ["adoption_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption.tla)\" " +
          "\"$(location adoption_liveness.cfg)\" " +
          "\"$@\" pass",
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_adoption_wedge",
    srcs = _JRE_SRCS + [
        "adoption.tla",
        "adoption_wedge.cfg",
    ],
    outs = ["adoption_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption.tla)\" " +
          "\"$(location adoption_wedge.cfg)\" " +
          "\"$@\" fail",
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_adoption_resurrection",
    srcs = _JRE_SRCS + [
        "adoption.tla",
        "adoption_resurrection.cfg",
    ],
    outs = ["adoption_resurrection.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption.tla)\" " +
          "\"$(location adoption_resurrection.cfg)\" " +
          "\"$@\" fail",
    tools = ["//bazel/tla:tlc.sh"],
)

# Protocol 2 (ADR embervm/006): the bank/relight generation-pairing spec, added by
# the ADR embervm/014 PR 5 TLA follow-through. Two positive safety configs (the
# shared reuse lane and the isolated single-use lane) and one NEGATIVE config that
# proves the model still catches the pre-PR-#3770 generation regression.
genrule(
    name = "tlc_bank_relight",
    srcs = _JRE_SRCS + [
        "bank_relight.tla",
        "bank_relight.cfg",
    ],
    outs = ["bank_relight.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location bank_relight.tla)\" " +
          "\"$(location bank_relight.cfg)\" " +
          "\"$@\" pass",
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_bank_relight_isolated",
    srcs = _JRE_SRCS + [
        "bank_relight.tla",
        "bank_relight_isolated.cfg",
    ],
    outs = ["bank_relight_isolated.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location bank_relight.tla)\" " +
          "\"$(location bank_relight_isolated.cfg)\" " +
          "\"$@\" pass",
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_bank_relight_regress",
    srcs = _JRE_SRCS + [
        "bank_relight.tla",
        "bank_relight_regress.cfg",
    ],
    outs = ["bank_relight_regress.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location bank_relight.tla)\" " +
          "\"$(location bank_relight_regress.cfg)\" " +
          "\"$@\" fail",
    tools = ["//bazel/tla:tlc.sh"],
)

# The spec sources the layer-1 vocabulary sync test (Task 3) reads. vocabulary.exs
# is globbed now so Task 3 does not have to touch this BUILD; the glob tolerates
# its current absence. //bazel/erlang is granted visibility because mix_test_smoke
# (the general test lane, //bazel/erlang) stages these into the mix sandbox so the
# vocabulary test can read adoption.tla + vocabulary.exs there.
filegroup(
    name = "spec_files",
    srcs = glob([
        "*.tla",
        "*.cfg",
        "vocabulary.exs",
    ]),
    visibility = [
        "//bazel/erlang:__pkg__",
        "//projects/embervm:__subpackages__",
    ],
)
