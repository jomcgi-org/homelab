# EmberVM adoption-protocol TLA+ spec, checked by TLC in CI (ADR embervm/006).
#
# Four genrules run the //bazel/tla:tlc.sh driver over adoption.tla with a
# different cfg each: rich safety, positive liveness, and two NEGATIVE modes
# (expectation "fail") that prove the model still reproduces a historical bug, so
# the driver fails the build if TLC finds NO violation (the model went blind).
# Safety and liveness are split across cfgs because exhaustive liveness over the
# rich safety graph is too slow for CI; see README.md for the cfg matrix.
#
# The JRE is staged as its whole :jre filegroup (bin/java needs its lib/ siblings)
# plus the :bin/java anchor, mirroring how //projects/embervm/control stages
# @otp's :otp filegroup + :Install anchor. tla2tools.jar comes from @tla2tools.
#
# No no-remote-cache tag: the check is deterministic in (spec, cfg, toolchain),
# so a cache hit is a correct skip and a full TLC re-run only happens when one of
# those changes. That is exactly the ADR's cost model (full checking on spec
# change, not every push).

_JRE_SRCS = [
    "@temurin21_jre_linux_amd64//:jre",
    "@temurin21_jre_linux_amd64//:bin/java",
    "@tla2tools//file",
    "adoption.tla",
]

_JAVA = "@temurin21_jre_linux_amd64//:bin/java"

_JAR = "@tla2tools//file"

genrule(
    name = "tlc_adoption",
    srcs = _JRE_SRCS + ["adoption.cfg"],
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
    srcs = _JRE_SRCS + ["adoption_liveness.cfg"],
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
    srcs = _JRE_SRCS + ["adoption_wedge.cfg"],
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
    srcs = _JRE_SRCS + ["adoption_resurrection.cfg"],
    outs = ["adoption_resurrection.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption.tla)\" " +
          "\"$(location adoption_resurrection.cfg)\" " +
          "\"$@\" fail",
    tools = ["//bazel/tla:tlc.sh"],
)

# The spec sources the layer-1 vocabulary sync test (Task 3) reads. vocabulary.exs
# is globbed now so Task 3 does not have to touch this BUILD; the glob tolerates
# its current absence.
filegroup(
    name = "spec_files",
    srcs = glob([
        "*.tla",
        "*.cfg",
        "vocabulary.exs",
    ]),
    visibility = ["//projects/embervm:__subpackages__"],
)
