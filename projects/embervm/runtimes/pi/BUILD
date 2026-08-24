# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# EmberVM pi runtime base image. A session-class guest whose PID 1 is the SAME
# stdlib-python shim the claude runtime uses, answering the same frozen
# HTTP-over-vsock guest contract on port 1027, but carrying ONLY the pi coding
# agent.
#
# Every layer except the pi tar is reused verbatim from ../claude rather than
# rebuilt here, so the two images cannot drift in the shim, the trust-record
# seed, or the guest-init binary. That reuse is the point: the shim already
# constructs its claude, codex and pi adapters lazily (ProcessManager only
# spawns the CLI a turn actually selects), so a pi-only image needs no shim
# fork, and forking one would be a second copy of 3700 lines policed by nothing.

# First-class web research tools for Pi. The extension deliberately uses curl
# through pi.exec rather than runtime fetch(), so every request stays inside
# EmberVM's brokered egress policy.
#
# Note which variable curl actually reads. For an http:// URL it honors
# lowercase http_proxy ONLY, ignoring uppercase HTTP_PROXY on purpose (a CGI
# environment can forge that one from a Proxy: header). The shared shim exports
# both cases for exactly this reason, and the extension also passes --proxy
# explicitly, because a guest has no NIC and a request that misses the lane
# fails as an unresolvable host rather than as a routing error.
tar(
    name = "web_research_extension_tar",
    srcs = ["web-research.ts"],
    mtree = [
        "./usr/share/ember-pi/extensions/web-research.ts type=file content=$(execpath web-research.ts) mode=0644 uid=0 gid=0",
    ],
    # The larger claude runtime can still route a qwen turn through the shared
    # shim, so it must carry the same extension path as the pi-only image.
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)

# This image's prewarm list (#4423), read by the shared shim's
# ProcessManager._read_prewarm_clis at /usr/share/ember-shim/prewarm-clis.
# Only pi: the claude and codex CLIs are deliberately absent from this image,
# and prewarming a binary that does not exist would fail every boot. Task-class
# guests have no env delivery (#4429), so this baked file, not initEnv, is how
# the list reaches the guest.
tar(
    name = "prewarm_clis_tar",
    srcs = ["prewarm-clis"],
    mtree = [
        "./usr/share/ember-shim/prewarm-clis type=file content=$(execpath prewarm-clis) mode=0644 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64-only. Unlike ../claude this is not forced by the CLI (pi ships
    # dual-arch), but by the guest-init tar below, which is built through an
    # amd64 platform transition, and by there being no aarch64 node to run it.
    # See apko.yaml. arm64 = False with a per-arch `tars` entry is the supported
    # combination; arm64 = False with multiarch_tars fails at PUSH time, after
    # PR CI is already green.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_pi_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/pi",
    tars = [
        # The shared shim, trust-record seed and guest-init, taken from the
        # claude runtime package as built artifacts so both images ship byte
        # identical copies.
        "//projects/embervm/runtimes/claude:shim_tar",
        "//projects/embervm/runtimes/claude:guest_config_tar",
        "//projects/embervm/runtimes/claude:guest_init_tar_amd64",
        ":web_research_extension_tar",
        ":prewarm_clis_tar",
        # The pi coding agent, a self-contained Bun-compiled ELF from the GitHub
        # release (see pi_coding_agent in MODULE.bazel), plus the theme JSONs and
        # wasm sidecar it loads relative to argv0. The ONLY CLI in this image:
        # claude_code_patched_tar_amd64 and @codex_cli//:tar_amd64 are
        # deliberately absent, and that absence is this image's entire reason to
        # exist (200 MB of the claude runtime's 300 MB compressed).
        "@pi_coding_agent//:tar_amd64",
    ],
)

# Guard: this image's package set is documented as deliberately IDENTICAL to the
# claude runtime's, so that the only difference between the two guests is which
# CLIs exist. A package added to one and not the other silently breaks that
# claim, and the failure would surface as a pi turn missing a binary the claude
# runtime has, inside a guest, which is the most expensive place to see it.
# Divergence is allowed, it just has to be deliberate: add the package to the
# EXPECTED_DIVERGENCE set in the test with a reason.
# Hand-registered: gazelle does not generate py_test targets in this repo.
py_test(
    name = "package_parity_test",
    srcs = ["package_parity_test.py"],
    data = [
        "apko.yaml",
        "//projects/embervm/runtimes/claude:apko_yaml",
    ],
    imports = ["."],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
