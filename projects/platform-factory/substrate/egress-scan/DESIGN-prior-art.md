# Prior art for the egress scanner

Status: evaluation result (2026-11-24). No code or cluster change. It belongs to
[ARCHITECTURE.md](ARCHITECTURE.md) and follows [DESIGN-detectors.md](DESIGN-detectors.md).
The build plan in that file is pending this result. Joe asked whether the
scanner rebuilds something that exists.

## Result

- No project covers the whole design. Two projects cover a large part of it.
  Pipelock has the best credential detection and decoding we found. Betterleaks
  has the best rule set to embed.
- Pipelock is a forward proxy, not an `ext_authz` service. Its detector lives in
  Go `internal/` packages, so no other module can import it. It does expose
  `POST /api/v1/scan` (kind `dlp`) over HTTP, and that fits our engine contract.
- agentgateway's own guardrails run on LLM routes only. They cannot replace the
  scanner for plain HTTP egress.
- Nothing found inspects a git push as code, runs a licensed engine in a
  sandbox, or keys a verdict on an actor identity.
- Verdicts: Pipelock, evaluate as a detector service. Betterleaks, reuse as a
  library (one proxy already does). Everything else, ignore.

## Pipelock

Repository `pipelab-org/pipelock` (moved from `luckyPipewrench/pipelock`).

| Item | Finding |
| --- | --- |
| Licence | Core Apache-2.0. `enterprise/` is Elastic License 2.0. Per-agent profiles, per-agent routing and the dashboard need a paid licence (Pro) |
| Maturity | Created 2026-02-08. 1,021 stars. v3.8.0 on 2026-11-10, 53 releases. About 2,190 commits and 547,000 lines of Go outside tests. One maintainer (about 2,030 commits). `CLAUDE.md` and `AGENTS.md` in the tree: heavy AI assistance |
| Shape | Forward proxy (`HTTPS_PROXY`), fetch proxy, WebSocket proxy, MCP and A2A wrappers. It scans a CONNECT tunnel only with its own TLS interception. No `ext_authz`, `ext_proc` or ICAP mode (a search of the tree found none) |
| Detector | Its own regexes: 74 built-in DLP patterns with checksums, an entropy check, and BIP-39 seed phrases. It does not use Gitleaks or Betterleaks |
| Decoding | The widest we found: base64, base64url, base32, hex, delimited hex, multi-layer percent encoding (500 rounds), HTML entities, JSON unicode escapes, Unicode tags, mixed chains. Gzip, ZIP and Office bodies are unpacked within a size cap. Budgets bound the cost |
| Bodies | `request_body_scanning.max_body_bytes` (5 MiB). A body over the limit is blocked (fail closed). Stacked codings, brotli and zstd are blocked. A scan failure blocks |
| Policy | Modes strict, balanced and audit. Per-destination rules and allowlists. Core credential patterns always block, even in audit |
| Git | A smart-HTTP push allowlist (`allowed_push_repos`) and a local `git scan-diff` for hooks and CI. It does not unpack a pushed pack. Its docs say blob scanning is future work |
| Identity | An agent name in the request, and per-agent profiles (paid). SVID and AARP appraisal is verifier-side only. It does not key an inline verdict on a SPIFFE identity |
| TLS | Own CA and leaf certificates. We already terminate TLS at the gateway, so we would not use it |
| Evidence | Signed action receipts, a flight recorder, Prometheus metrics (`pipelock_scan_api_*`) |
| Quality | 3,490 test files, fuzz and hardening workflows, OpenSSF Scorecard, signed releases with SBOMs. Security policy with response targets |
| Library use | Not possible. The scanner is in `internal/`. `sdk/` holds receipt verifiers only |

### Run on the corpus

The binary is the `v3.8.0` release (checksum matched `checksums.txt`; I did not
verify the cosign signature). It ran in an empty network namespace, with the
balanced preset and the Scan API on loopback. The same 63 fake samples as in
DESIGN-detectors.md went in as `dlp` scans: the body, and the body plus decoded
JSON strings (the two gave the same verdicts).

| Result | Gitleaks 8.24.3 | Betterleaks | Pro engine | Pipelock |
| --- | --- | --- | --- | --- |
| Secrets found, of 45 | 29 | 38 | 32 | 37 |
| False positives, of 18 | 0 | 0 | 0 | 1 |
| Only this tool found it | none | 7 encoded | 2 headers | 3: `split-across-lines`, `jwt` (malformed), `db-url` |

- All four together found 43 of 45. The two misses are `split-lines-json-concat`
  and `gzip-b64-github`.
- Pipelock found every base64, double base64, hex and percent-encoded sample
  that Betterleaks found, and an encoded private key.
- Pipelock missed `auth-bearer-40`, `auth-basic`, `kubeconfig-token`,
  `k8s-secret-b64-pw`, `generic-password` and `slack-webhook`. The Pro engine
  or a regex tool found each one.
- The false positive: a kubeconfig with `server: https://10.x.x.x:6443` fired
  `SSRF-Private-IP`. The scan treats a private address in text as a finding.
  That rule needs tuning before it sits on a general body.
- Latency of the Scan API on loopback, median of 9 (HTTP included):

| Body | Prose | Keyword-heavy | Base64 |
| --- | --- | --- | --- |
| 1 KiB | 3 ms | 2 ms | 9 ms |
| 64 KiB | 29 ms | 74 ms | 69 ms |
| 511 KiB | 208 ms | 611 ms | 666 ms |

A 64 KiB body fits the 300 ms deadline. The limit of the Scan API text field is
512 KiB, so large bodies need windows here too.

### Verdict: evaluate as a detector service, do not import

- Do not fork it or depend on a library. There is none, and the code is one
  maintainer's 547,000 lines.
- Its `dlp` scan is an HTTP detector with the shape of our engine contract. A
  small adapter from our scanner (text in, findings out) lets us run it as one
  more tier. Run it in observe mode first and compare counters. This needs no
  decoder chain of our own, because Pipelock decodes more than the chain planned
  in DESIGN-detectors.md.
- Contribute to it, if Joe wants, in a small way: an `ext_authz` front, a
  windowed scan for large bodies, and Betterleaks rules. Ask in an issue first.
  The maintainer is one person, and the project prefers its own engine.
- Risk to name: one more dependency on the deny path, with a paid tier next to
  the free one. Keep it a tier with a failure mode, as the other tiers are.

## Other candidates

| Candidate | Finding | Verdict |
| --- | --- | --- |
| AgentShield (`kamuimk/agentshield`, Rust) | Apache-2.0, 1 star, one author, 25,000 lines. A desktop forward proxy with its own 15 to 20 regexes, base64 and JSON-escape decoding, a 10 MiB body cap, and a deny on any budget overrun or compressed upload. Domains on its allowlist skip DLP. No per-actor identity, no Prometheus, no git. Last release 2026-02-19 | Ignore. Copy one idea: a scan that cannot finish is a deny |
| agentgateway guardrails (v1.7.0) | `ai.promptGuard` and the webhook guardrail run on LLM backends only. The built-in detectors are PII (SSN, card, phone, email), with no secret patterns and no decoding. No git support. No open issue asks for a secret scanner | Ignore for this purpose |
| agentgateway `extAuthz` and `extProc` | `extAuthz` takes `includeRequestBody.maxRequestBytes` (default 8 KiB). With `allowPartialMessage: true` only the prefix is scanned, so padding hides a secret. Our `false` returns 413. It passes a per-route `context` map to the server, which answers the per-route question in DESIGN-large-bodies.md. `extProc` can stream bodies and responses, with a 2 MiB buffer default and a fail-open gap after the first body byte (upstream issue 3836) | Keep `extAuthz`. `extProc` is the fallback for responses |
| ICAP, c-icap, Squid | Frameworks. The shipped services are antivirus and URL filters. Hobby DLP adapters only. Fortra's gateway is commercial | Ignore |
| Envoy filters | An archived Wasm filter that calls a paid cloud DLP. Sample `ext_proc` servers | Ignore |
| Kong, NGINX, Istio | No open source plugin that scans bodies for secrets (GitHub search only, not the plugin hubs) | Ignore |
| `finos/git-proxy` | Apache-2.0, 270 stars, FINOS graduated, v2.3.1 (2026-11-16). The one proxy that handles `git-receive-pack`: it clones, applies the pack, diffs, then optionally runs the Gitleaks binary (off by default) before it forwards. Node, a clone per push, a user database and an approval flow | Design reference for git-as-code. Do not adopt |
| GitLab secret push protection | Pre-receive hook, diff only, small high-confidence rule set. A scan timeout lets the push through (fail open). A user can skip it | Avoid its fail-open timeout |
| GitHub push protection | Blocks pushes, API and UI commits. Users with write access can bypass it. The engine is undocumented | Ignore |
| ggshield | MIT, 2,024 stars. A CLI that calls the GitGuardian service. Hooks, no proxy | Ignore |
| TruffleHog | AGPL-3.0, 29,600 stars | Ignore: licence |
| detect-secrets, LLM Guard, Presidio | Python. detect-secrets is Apache-2.0 and slow-moving. LLM Guard wraps detect-secrets. Presidio is PII | Ignore |
| Small agent firewalls (`decoyrail`, `agent-egress-firewall`, `agent-egress-dlp`, `privacy-filter`, `censgate/redact`) | One to 340 stars, weeks old. `privacy-filter` (Go) reuses the Gitleaks rules and redacts instead of denying | Ignore. `decoyrail` has one idea: spot a known decoy value in any encoding |

## Who embeds Betterleaks

- `aiproxer/aiproxer` (MIT, 24 stars) is a proxy that embeds the library. It
  pins `v2.0.0-rc.2` and has an adapter (`secretguard/betterleaks_adapter.go`)
  with a deadline, a finding cap, a source-failure class and a projection that
  never copies the match text. It is the one reference for our adapter. It is
  small and 4 weeks old.
- Other embedders are tools and hooks, not gateways: `cquark7/withhold`,
  `entireio/cli`, `Infisical/cli`, `leaktk/leaktk` and `chainloop-dev/chainloop`.
- No `ext_authz` service, ICAP service or git gateway embeds it.
- Verdict: reuse the library, as DESIGN-detectors.md plans, with the adapter
  limits listed there.

## What is unique here

| Piece | Exists elsewhere? |
| --- | --- |
| Regex credential detection with decoding | Yes. Pipelock is stronger on decoding than our plan. Betterleaks has the larger rule set |
| An `ext_authz` hook with a body | Yes, in agentgateway and Envoy. No open source credential scanner uses it |
| TLS interception | Yes, at our gateway |
| A licensed engine in a single-use gVisor child behind `engine-gate`, with no egress | No. Nothing found runs a scan engine in a sandbox per request |
| A verdict keyed on the actor identity | No. The nearest is Pipelock's paid per-agent profile, which keys on a name and a network range |
| Deny when a tier is out, with alerts and a planned HA roll | Partly. AgentShield and Pipelock fail closed on a budget or parse failure. None alerts on an outage of a tier or plans HA for it |
| Git push scanned as code, with only high-confidence rules blocking | No. `git-proxy` unpacks and diffs but is a heavy Node service that runs Gitleaks off by default. Pipelock checks a repository allowlist only |
| Windowed scan on parallel children | No |

## Decision for Joe

1. Keep the Betterleaks build plan, pending this result. PR 2 (our decoder chain)
   is the part Pipelock makes redundant if we run it as a tier.
2. Recommended: run a one-day observe trial of Pipelock's Scan API as a third tier
   before the decoder PR. It needs an adapter and one pod, and no decoder chain.
3. Alternative: build the Betterleaks tier and our own decoder, and treat
   Pipelock as a reference only.
4. Contribute upstream only after the trial, and only small changes.
