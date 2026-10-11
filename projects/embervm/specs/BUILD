# EmberVM protocol TLA+ specs, checked by TLC in CI (ADR embervm/006).
#
# Eleven specs, sixty-four genrules run the //bazel/tla:tlc.sh driver:
#   adoption.tla (protocol 1: VM lifecycle + adoption) with four cfgs: rich
#     safety, positive liveness, and two NEGATIVE modes (expectation "fail") that
#     prove the model still reproduces a historical bug.
#   adoption_trace.tla (Tier B trace validation, issue #6415, staged
#     default-off) with three cfgs: a pass fixture for the restart-plus-destroy
#     happy path and two NEGATIVE fixtures for the acceptance deviations.
#   bank_relight.tla (protocol 2: session bank/relight generation pairing, added by
#     the ADR embervm/014 PR 5 follow-through) with three cfgs: a shared-lane safety
#     config, an isolated single-use safety config, and one NEGATIVE mode proving
#     the model still catches the pre-PR-#3770 generation regression.
#   quota.tla (protocol 3: the fail-closed per-principal daily quota gate, shared
#     by the router submit path and the dispatcher fair queue) with three cfgs: a
#     shared safety config, a zero-budget config that makes the hard-stop
#     invariant non-vacuous, and one NEGATIVE submit-only mode reproducing the
#     D12.3 overshoot hole.
#   session_create.tla (SessionManager create starvation, issue #5051) with
#     positive safety and liveness configs plus one NEGATIVE mode reproducing
#     the blocking do_reconcile mailbox wedge.
#   generation_issuance.tla (protocol 4: generation issuance authority, issue
#     #4700) with four cfgs: a rich adversarial safety config, a positive
#     liveness config, and two NEGATIVE modes reproducing the two halves of the
#     blessing-watermark false-quarantine history: the pre-ADR-017 fail-closed-
#     both-ways checkpoint abort and the pre-ADR-014 fenced-writer deadlock.
#   stateful.tla (stateful lifecycle, checkpoint recovery, destroy redrive, and
#     writable attach singleton) with safety and liveness configs, two NEGATIVE
#     incident wedges, and one NEGATIVE destroy-escape design comparison.
#   session_lineage.tla (issue #4701: session lineage handoff) with one rich
#     safety config and four NEGATIVE modes, one for each retained invariant.
#   warmth_gc.tla (issue #4705: warmth GC racing the durable session-workspace
#     tier) with one exhaustive safety config and seven NEGATIVE modes, one per
#     guard, covering protected-reference deletion, silent empty resume,
#     deletion on an inconsistent inventory, the two eligibility arms
#     (parked expiry and the per-kind age floor), and the newest-reference
#     retention of the DISPOSABLE stateful tier.
#   dual_wake.tla (issue #4702: concurrent CP and brick wakes) with twenty
#     cfgs: three safety bounds, liveness, six single-mutation negatives, four
#     non-vacuity witnesses, the current-main two-VM witness, and two PASS sensitivity checks
#     (origin skip removal alone and orphan-before-adoption ordering alone),
#     plus a composite negative disabling both redundant orphan guards,
#     plus two liveness wedges (bare "fail": Leak breaks EventualAccounting,
#     SilentFailure breaks RequestsResolve).
#   admission.tla (memory admission across bricks after the 2026-10-10
#     OOMKills: noded's atomic ticket ledger and the CP's pending claims) with
#     six cfgs: a safety config, positive liveness with the frontier walk on
#     and off, and three NEGATIVE modes reproducing observed admission under
#     lazy guest growth, a check-then-act double admit, and a single attempt
#     that strands a request.
# The driver fails the build if a "fail" mode finds NO violation (the model went
# blind). Safety and liveness are split across cfgs because exhaustive liveness over
# the rich safety graph is too slow for CI; see README.md for the cfg matrix.
#
# The JRE is staged as its whole :jre filegroup (bin/java needs its lib/ siblings)
# plus the :bin/java anchor, mirroring how //projects/embervm/control stages
# @otp's :otp filegroup + :Install anchor. tla2tools.jar comes from @tla2tools.
#
# Every TLC genrule carries tags = ["verification"], which is how
# bazel/tools/ci/affected-targets.sh finds a suite that is not a Bazel test rule
# (#5538). Without it, editing a .tla or .cfg reaches no test() and the PR
# model-checks nothing while reporting green; the merge queue's //... is the
# only thing that would have caught it.
#
# No no-remote-cache tag: the check is deterministic in (spec, cfg, toolchain),
# so a cache hit is a correct skip and a full TLC re-run only happens when one of
# those changes. That is exactly the ADR's cost model (full checking on spec
# change, not every push).

# The staged JRE + jar, shared by every TLC genrule. The spec .tla each genrule
# checks is added per-target (adoption.tla, bank_relight.tla, or quota.tla), so
# this list carries only the toolchain, not a spec.
_JRE_SRCS = [
    "@temurin21_jre_linux_amd64//:jre",
    "@temurin21_jre_linux_amd64//:bin/java",
    "@tla2tools//file",
]

_JAVA = "@temurin21_jre_linux_amd64//:bin/java"

_JAR = "@tla2tools//file"

genrule(
    name = "tlc_dual_wake_vm_crash",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_vm_crash.cfg",
    ],
    outs = ["dual_wake_vm_crash.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_vm_crash.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_faults",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_faults.cfg",
    ],
    outs = ["dual_wake_faults.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_faults.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake.cfg",
    ],
    outs = ["dual_wake.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_liveness",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_liveness.cfg",
    ],
    outs = ["dual_wake_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_liveness.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_wrong_winner",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_wrong_winner.cfg",
    ],
    outs = ["dual_wake_wrong_winner.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_wrong_winner.cfg)\" " +
          "\"$@\" fail OneWinnerPerFlight",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_untracked",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_untracked.cfg",
    ],
    outs = ["dual_wake_untracked.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_untracked.cfg)\" " +
          "\"$@\" fail NoUntrackedServing",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_reap_activator",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_reap_activator.cfg",
    ],
    outs = ["dual_wake_reap_activator.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_reap_activator.cfg)\" " +
          "\"$@\" fail NoLegitActivatorWakeReaped",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_unguarded_orphans",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_unguarded_orphans.cfg",
    ],
    outs = ["dual_wake_unguarded_orphans.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_unguarded_orphans.cfg)\" " +
          "\"$@\" fail NoLegitActivatorWakeReaped",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_early_register",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_early_register.cfg",
    ],
    outs = ["dual_wake_early_register.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_early_register.cfg)\" " +
          "\"$@\" fail PublishAfterReady",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_silent_failure",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_silent_failure.cfg",
    ],
    outs = ["dual_wake_silent_failure.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_silent_failure.cfg)\" " +
          "\"$@\" fail NoDeadSplice",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_leak",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_leak.cfg",
    ],
    outs = ["dual_wake_leak.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_leak.cfg)\" " +
          "\"$@\" fail NoLeak",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_offline",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_offline.cfg",
    ],
    outs = ["dual_wake_offline.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_offline.cfg)\" " +
          "\"$@\" fail NeverOfflineDelivery",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_return_adoption",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_return_adoption.cfg",
    ],
    outs = ["dual_wake_return_adoption.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_return_adoption.cfg)\" " +
          "\"$@\" fail NeverReturnAdoption",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_duplicate_resolved",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_duplicate_resolved.cfg",
    ],
    outs = ["dual_wake_duplicate_resolved.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_duplicate_resolved.cfg)\" " +
          "\"$@\" fail NeverDuplicateResolved",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_loud_failure",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_loud_failure.cfg",
    ],
    outs = ["dual_wake_loud_failure.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_loud_failure.cfg)\" " +
          "\"$@\" fail NeverLoudFailure",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_two_vms",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_two_vms.cfg",
    ],
    outs = ["dual_wake_two_vms.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_two_vms.cfg)\" " +
          "\"$@\" fail AtMostOneVMPerWorkload",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_origin_skip_disabled",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_origin_skip_disabled.cfg",
    ],
    outs = ["dual_wake_origin_skip_disabled.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_origin_skip_disabled.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_orphans_first",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_orphans_first.cfg",
    ],
    outs = ["dual_wake_orphans_first.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_orphans_first.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_accounting_wedge",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_accounting_wedge.cfg",
    ],
    outs = ["dual_wake_accounting_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_accounting_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_dual_wake_requests_wedge",
    srcs = _JRE_SRCS + [
        "dual_wake.tla",
        "dual_wake_requests_wedge.cfg",
    ],
    outs = ["dual_wake_requests_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location dual_wake.tla)\" " +
          "\"$(location dual_wake_requests_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

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
    tags = ["verification"],
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
    tags = ["verification"],
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
    tags = ["verification"],
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
    tags = ["verification"],
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
    tags = ["verification"],
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
    tags = ["verification"],
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
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_quota",
    srcs = _JRE_SRCS + [
        "quota.tla",
        "quota.cfg",
    ],
    outs = ["quota.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location quota.tla)\" \"$(location quota.cfg)\" \"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_quota_zero",
    srcs = _JRE_SRCS + [
        "quota.tla",
        "quota_zero.cfg",
    ],
    outs = ["quota_zero.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location quota.tla)\" \"$(location quota_zero.cfg)\" \"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_quota_submit_only",
    srcs = _JRE_SRCS + [
        "quota.tla",
        "quota_submit_only.cfg",
    ],
    outs = ["quota_submit_only.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location quota.tla)\" \"$(location quota_submit_only.cfg)\" \"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_create",
    srcs = _JRE_SRCS + [
        "session_create.tla",
        "session_create.cfg",
    ],
    outs = ["session_create.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_create.tla)\" " +
          "\"$(location session_create.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_create_liveness",
    srcs = _JRE_SRCS + [
        "session_create.tla",
        "session_create_liveness.cfg",
    ],
    outs = ["session_create_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_create.tla)\" " +
          "\"$(location session_create_liveness.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_create_wedge",
    srcs = _JRE_SRCS + [
        "session_create.tla",
        "session_create_wedge.cfg",
    ],
    outs = ["session_create_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_create.tla)\" " +
          "\"$(location session_create_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Protocol 4 (issue #4700): the generation issuance authority spec. One rich
# adversarial safety config (all guards on, rogue second-writer budgeted, grant
# expiry armed), one positive liveness config on lean bounds, and two NEGATIVE
# configs that prove the model still catches each half of the
# blessing-watermark false-quarantine history.
genrule(
    name = "tlc_generation_issuance",
    srcs = _JRE_SRCS + [
        "generation_issuance.tla",
        "generation_issuance.cfg",
    ],
    outs = ["generation_issuance.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location generation_issuance.tla)\" " +
          "\"$(location generation_issuance.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_generation_issuance_liveness",
    srcs = _JRE_SRCS + [
        "generation_issuance.tla",
        "generation_issuance_liveness.cfg",
    ],
    outs = ["generation_issuance_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location generation_issuance.tla)\" " +
          "\"$(location generation_issuance_liveness.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_generation_issuance_heal_wedge",
    srcs = _JRE_SRCS + [
        "generation_issuance.tla",
        "generation_issuance_heal_wedge.cfg",
    ],
    outs = ["generation_issuance_heal_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location generation_issuance.tla)\" " +
          "\"$(location generation_issuance_heal_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_generation_issuance_lag_wedge",
    srcs = _JRE_SRCS + [
        "generation_issuance.tla",
        "generation_issuance_lag_wedge.cfg",
    ],
    outs = ["generation_issuance_lag_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location generation_issuance.tla)\" " +
          "\"$(location generation_issuance_lag_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Stateful lifecycle and both September 2026 incident wedges. The fifth target
# checks the rejected "owner missing means destroyed" design and must fail
# NoDestroyBeforeConfirm, documenting why production escapes to failed.
genrule(
    name = "tlc_stateful",
    srcs = _JRE_SRCS + [
        "stateful.tla",
        "stateful.cfg",
    ],
    outs = ["stateful.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location stateful.tla)\" " +
          "\"$(location stateful.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_stateful_liveness",
    srcs = _JRE_SRCS + [
        "stateful.tla",
        "stateful_liveness.cfg",
    ],
    outs = ["stateful_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location stateful.tla)\" " +
          "\"$(location stateful_liveness.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_stateful_destroying_wedge",
    srcs = _JRE_SRCS + [
        "stateful.tla",
        "stateful_destroying_wedge.cfg",
    ],
    outs = ["stateful_destroying_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location stateful.tla)\" " +
          "\"$(location stateful_destroying_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_stateful_attach_wedge",
    srcs = _JRE_SRCS + [
        "stateful.tla",
        "stateful_attach_wedge.cfg",
    ],
    outs = ["stateful_attach_wedge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location stateful.tla)\" " +
          "\"$(location stateful_attach_wedge.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_stateful_destroy_escape_unsafe",
    srcs = _JRE_SRCS + [
        "stateful.tla",
        "stateful_destroy_escape_unsafe.cfg",
    ],
    outs = ["stateful_destroy_escape_unsafe.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location stateful.tla)\" " +
          "\"$(location stateful_destroy_escape_unsafe.cfg)\" " +
          "\"$@\" fail",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Issue #4701: one exhaustive positive safety check and one focused NEGATIVE
# mutation per invariant. Each negative cfg lists only TypeOK and its intended
# invariant so an unrelated earlier violation cannot satisfy expectation fail.
genrule(
    name = "tlc_session_lineage",
    srcs = _JRE_SRCS + [
        "session_lineage.tla",
        "session_lineage.cfg",
    ],
    outs = ["session_lineage.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_lineage.tla)\" " +
          "\"$(location session_lineage.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_lineage_loss",
    srcs = _JRE_SRCS + [
        "session_lineage.tla",
        "session_lineage_loss.cfg",
    ],
    outs = ["session_lineage_loss.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_lineage.tla)\" " +
          "\"$(location session_lineage_loss.cfg)\" " +
          "\"$@\" fail NoWorkspaceLoss",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_lineage_double_heir",
    srcs = _JRE_SRCS + [
        "session_lineage.tla",
        "session_lineage_double_heir.cfg",
    ],
    outs = ["session_lineage_double_heir.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_lineage.tla)\" " +
          "\"$(location session_lineage_double_heir.cfg)\" " +
          "\"$@\" fail NeverTwoLiveHeirs",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_lineage_nonterminal",
    srcs = _JRE_SRCS + [
        "session_lineage.tla",
        "session_lineage_nonterminal.cfg",
    ],
    outs = ["session_lineage_nonterminal.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_lineage.tla)\" " +
          "\"$(location session_lineage_nonterminal.cfg)\" " +
          "\"$@\" fail InheritanceOnlyFromTerminal",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_session_lineage_silent_merge",
    srcs = _JRE_SRCS + [
        "session_lineage.tla",
        "session_lineage_silent_merge.cfg",
    ],
    outs = ["session_lineage_silent_merge.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location session_lineage.tla)\" " +
          "\"$(location session_lineage_silent_merge.cfg)\" " +
          "\"$@\" fail ReconnectDetectsDivergence",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Issue #4705: warmth GC racing the DURABLE session-workspace tier. One
# exhaustive positive safety config plus one focused NEGATIVE mutation per
# guard. Each negative cfg lists only TypeOK and its intended invariant, so an
# unrelated earlier violation cannot satisfy expectation fail. Two negatives
# (unexpired, age_floor) target the same eligibility invariant through its two
# independent arms, and two (protected, stale_fleet) target the same protection
# invariant through the reference hold and the fleet-freshness assumption.
genrule(
    name = "tlc_warmth_gc",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc.cfg",
    ],
    outs = ["warmth_gc.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_protected",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_protected.cfg",
    ],
    outs = ["warmth_gc_protected.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_protected.cfg)\" " +
          "\"$@\" fail NoProtectedDeletion",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_stale_fleet",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_stale_fleet.cfg",
    ],
    outs = ["warmth_gc_stale_fleet.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_stale_fleet.cfg)\" " +
          "\"$@\" fail NoProtectedDeletion",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_empty_resume",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_empty_resume.cfg",
    ],
    outs = ["warmth_gc_empty_resume.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_empty_resume.cfg)\" " +
          "\"$@\" fail NoSilentEmptyResume",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_aborted_sweep",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_aborted_sweep.cfg",
    ],
    outs = ["warmth_gc_aborted_sweep.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_aborted_sweep.cfg)\" " +
          "\"$@\" fail NoDeleteOnInconsistentInventory",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_unexpired",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_unexpired.cfg",
    ],
    outs = ["warmth_gc_unexpired.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_unexpired.cfg)\" " +
          "\"$@\" fail EligibilityRequiresExpiryAndAge",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_age_floor",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_age_floor.cfg",
    ],
    outs = ["warmth_gc_age_floor.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_age_floor.cfg)\" " +
          "\"$@\" fail EligibilityRequiresExpiryAndAge",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_warmth_gc_newest_ref",
    srcs = _JRE_SRCS + [
        "warmth_gc.tla",
        "warmth_gc_newest_ref.cfg",
    ],
    outs = ["warmth_gc_newest_ref.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location warmth_gc.tla)\" " +
          "\"$(location warmth_gc_newest_ref.cfg)\" " +
          "\"$@\" fail NewestStatefulRetainedWhileLive",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Memory admission across bricks (the 2026-10-10 OOMKills): noded's atomic
# reserved-model ticket ledger and the control plane's pending claims, placed
# from stale reports. One safety config, two liveness configs (frontier walk on
# and off), and one NEGATIVE mode per historical hole. Each negative cfg lists
# only TypeOK and its intended invariant, so an unrelated earlier violation
# cannot satisfy expectation fail.
genrule(
    name = "tlc_admission",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission.cfg",
    ],
    outs = ["admission.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_admission_liveness",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission_liveness.cfg",
    ],
    outs = ["admission_liveness.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission_liveness.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_admission_liveness_no_walk",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission_liveness_no_walk.cfg",
    ],
    outs = ["admission_liveness_no_walk.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission_liveness_no_walk.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_admission_observed",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission_observed.cfg",
    ],
    outs = ["admission_observed.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission_observed.cfg)\" " +
          "\"$@\" fail ResidentWithinBudget",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_admission_check_then_act",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission_check_then_act.cfg",
    ],
    outs = ["admission_check_then_act.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission_check_then_act.cfg)\" " +
          "\"$@\" fail NoOvercommit",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_admission_single_attempt",
    srcs = _JRE_SRCS + [
        "admission.tla",
        "admission_single_attempt.cfg",
    ],
    outs = ["admission_single_attempt.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location admission.tla)\" " +
          "\"$(location admission_single_attempt.cfg)\" " +
          "\"$@\" fail NoStrandedRequest",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# Tier B trace validation (issue #6415), staged default-off. adoption_trace.tla
# replays an exported SpecTrace window record by record and asserts the two
# ordering predicates the S6 runner verdicts on. The pass fixture is the happy
# path a deliberate CP restart plus destroy produces; the two NEGATIVE fixtures
# are the acceptance deviations (destroyed before node confirmation, same vm_id
# dispatched twice) and must name their invariant. Thin windows never reach TLC:
# the S6 runner returns vacuous below MinTraceEvents without checking, so there
# is deliberately no thin-window genrule here (TLC would pass it, which is the
# verdict the runner exists to withhold).
genrule(
    name = "tlc_adoption_trace_pass",
    srcs = _JRE_SRCS + [
        "adoption_trace.tla",
        "adoption_trace_pass.cfg",
    ],
    outs = ["adoption_trace_pass.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption_trace.tla)\" " +
          "\"$(location adoption_trace_pass.cfg)\" " +
          "\"$@\" pass",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_adoption_trace_destroy_before_confirm",
    srcs = _JRE_SRCS + [
        "adoption_trace.tla",
        "adoption_trace_destroy_before_confirm.cfg",
    ],
    outs = ["adoption_trace_destroy_before_confirm.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption_trace.tla)\" " +
          "\"$(location adoption_trace_destroy_before_confirm.cfg)\" " +
          "\"$@\" fail NoDestroyBeforeConfirm",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

genrule(
    name = "tlc_adoption_trace_double_dispatch",
    srcs = _JRE_SRCS + [
        "adoption_trace.tla",
        "adoption_trace_double_dispatch.cfg",
    ],
    outs = ["adoption_trace_double_dispatch.tlc.out"],
    cmd = "$(location //bazel/tla:tlc.sh) " +
          "\"$(location %s)\" " % _JAVA +
          "\"$(location %s)\" " % _JAR +
          "\"$(location adoption_trace.tla)\" " +
          "\"$(location adoption_trace_double_dispatch.cfg)\" " +
          "\"$@\" fail NoDoubleAssign",
    tags = ["verification"],
    tools = ["//bazel/tla:tlc.sh"],
)

# The S6 end-to-end TLC test (issue #6415) stages the shipped spec next to the
# runner-generated window module and runs real TLC over it. Exported as one
# file so the test can address it with $(rlocationpath ...); the filegroup
# below cannot serve that (rlocationpath needs a single file).
exports_files(
    ["adoption_trace.tla"],
    visibility = ["//projects/embervm:__subpackages__"],
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

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
