# bazel/semgrep

The repo's own Semgrep rules, plus the pinned engine and rule packs baked into
the semgrep guest image.

Nothing here scans code in CI. PR scanning is Semgrep Managed Scans, which is
configured on the Semgrep side and runs registry rules only. The Bazel test
layer that used to live here (`semgrep_test`, `semgrep_target_test`,
`semgrep_manifest_test`, the `semgrep` Gazelle language and the findings
uploader) never scanned anything on the Linux runner and was removed in
[#4777](https://github.com/jomcgi-org/homelab/issues/4777).
`bazel/ARCHITECTURE.md` section 8 has the rationale.

## Layout

| Path                                | What it is                                                                                         |
| ----------------------------------- | -------------------------------------------------------------------------------------------------- |
| `rules/`                            | About 93 custom rules, one subdirectory per language. `:local_rules` is the only target            |
| `tests/`                            | `# ruleid:` / `# ok:` annotation fixtures documenting each rule. No target runs them               |
| `guest/`                            | Tar layers for the semgrep guest image: the offline Pro engine and the merged rule set             |
| `third_party/semgrep_guest/`        | Hand-pinned Pro rule packs for the guest                                                           |
| `third_party/semgrep_experimental/` | Hand-pinned offline Pro engine for the guest                                                       |
| `third_party/semgrep_pro/`          | The `oci_archive` repository rule the guest pins use, plus the CI engine and pack pins (see below) |
| `third_party/semgrep/`              | CI OSS engine pins (see below)                                                                     |

## Where the rules run

`//bazel/semgrep/guest:rules_tar` lays `rules/` under `/etc/semgrep/rules/`
beside the Pro packs, preserving the per-language subdirectories because
basenames collide across them. The semgrep guest
(`projects/firecracker/semgrep/`) serves scans from that directory, and the
monolith's semgrep-scan MCP tool is the way in. A rule therefore flags a
problem when an agent or a person asks for a scan; it never blocks a PR.

## Leftovers

The CI engine repositories (`third_party/semgrep`, and the engine, rule-pack
and SCA-pack repositories declared by `third_party/semgrep_pro`) and
`.github/workflows/update-semgrep-pro.yaml`, which bumps their digests weekly,
have no consumer now. They stay until a follow-up removes them together with
their `MODULE.bazel` entries. The guest pins do not depend on them.
