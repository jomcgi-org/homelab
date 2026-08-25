# ADR 007: Aggregate Threat Model Index with Issue-Labeled Findings

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-08-24
**Relates to:** [Security 004: Public Read-Only Service Isolation](004-public-read-only-service-isolation.md), `projects/embervm/THREAT-MODEL.md`

---

## Problem

`projects/embervm/THREAT-MODEL.md` (merged 2026-08-22) is the first per-domain
STPA-Sec threat model in this repo. It works: section 4 tables six adversaries
against the substrate's control structure and section 5 ranks six findings
by blast radius, each tied to a GitHub issue. It also went stale within two
days. The 2026-08-24 revision had to close the dial-home hijack finding
(fixed by #4707/#5049) and move per-principal envelope encryption
from designed to armed in production, the bulk of what had been finding 1. A
model this deep drifts from the code as fast as the code moves; nothing kept
it current except a human rereading the whole document.

Meanwhile security residuals for every other surface live scattered across
GitHub issues with no roll-up. As of this writing seven open issues already
carry no shared label and span five domains with no per-domain model: MCP
gateway authorization (#4569, #4940), Kargo's promotion gate (#4745), the
semgrep engine skipping every target (#4777), and swarm's unenforced budget
(#4784), alongside embervm's own #4691 and #5255. A visitor asking "what is
open and how bad is it" has no page to read; they would have to know to
search issues at all.

`docs/security.md` describes the cluster's five baseline layers (perimeter,
CNI, admission, runtime, secrets) and a checklist for adding a service. It
says nothing about what is still open against that baseline.

## Decision

**1. A single aggregate index at `docs/THREAT-MODEL.md`** treats the homelab
as one organization. It names the assets, lists each hosted surface with its
exposure class and baseline controls, links the per-domain
`THREAT-MODEL.md` where one exists, and ranks the top open findings with a
sentence of context each. It restates nothing that a per-domain model owns:
rank and links only. embervm's finding 1 gets a line and a link to section 5
of its own document, not a second retelling of the jailer gap.

**2. Open security residuals carry the `security-finding` GitHub label.**
The label already exists (`security-finding`, `#B60205`) and seven issues
carry it today, across embervm, monolith, swarm, semgrep, Kargo, and MCP. The
label query (`gh issue list --label security-finding --state open`) is the
live list; the index page ranks only the top of it. Closing the issue is how
a finding leaves the list, the same trigger `projects/embervm/THREAT-MODEL.md`
section 7 already uses for its own refresh.

**3. New per-domain models land at `projects/<domain>/THREAT-MODEL.md`**
following embervm's shape: adversary tables, honest status labels (enforced
prod, shipped off, designed, none), a ranked residual section. They get
written where untrusted input lands, on demand rather than up front. Five
domains with open findings today have no per-domain model; this decision does
not commit to writing all five now, it commits to the shape the next one
takes when it is warranted.

**4. The index re-ranks by hand when a labeled finding opens or closes.**
Same trigger, same mechanism, as the per-domain docs: no scheduled job, no
generator, a human updates the page when the label query changes shape.

## Alternatives Considered

- **Full per-domain coverage plus a hand-maintained rollup that restates each
  model's findings.** Rejected. embervm's own document went stale in two
  days; a rollup that copies its findings verbatim doubles the staleness
  surface for every domain it covers, and there is no mechanism forcing the
  copy to update when the source does.
- **A generated rollup that extracts findings as JSON per domain**, the same
  judgment-and-mechanism split the `stpa` skill uses for STPA.md (findings
  extracted deterministically, markdown rendered by a jq template). Deferred,
  not rejected: it is a tooling project, and building it now is speculative
  against a hand-maintained index that has not yet been proven to fail.
  Revisit once the hand-maintained index has demonstrably gone stale twice.

## Security

Builds on the `docs/security.md` baseline; this ADR adds the residual-tracking
layer that document lacks. One cost is worth stating plainly: this repo is
public, so a page that ranks open weaknesses across every surface aggregates
information an attacker would otherwise have to collect one issue at a time.
Accepted, for three reasons. The underlying issues are already public; an
attacker with time to search labels already has the list. The aggregate keeps
mechanism detail in the per-domain document, where one exists, so the index
itself carries a sentence of context, not an exploit path. And a private list
contradicts how this repo already records work: GitHub Issues are the source
of truth for outstanding work, and that has never carried an exception for
security findings.

## Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Index drifts from the label query (a finding closes, the page does not update) | Medium | Low | Same failure mode the embervm document already has and the same fix: re-rank on the label-change trigger in decision 4 |
| A domain with untrusted input ships before its `THREAT-MODEL.md` exists | Medium | Medium | Decision 3 is on-demand by design; the aggregate index still lists the surface and its baseline controls even without a per-domain document, so the gap is visible rather than silent |
| Ranking read as an invitation rather than a status page | Low | Low | Findings link to the issue for detail; the index states blast radius, not exploit steps |

## References

| Resource | Relevance |
| --- | --- |
| `projects/embervm/STPA.md` (security lens; formerly `THREAT-MODEL.md`, absorbed by #5294) | The per-domain shape this ADR generalizes, and the staleness evidence behind decision 1 |
| `docs/security.md` | Baseline layers this index sits above; the document the aggregate index closes the residual-tracking gap in |
| [Security 004: Public Read-Only Service Isolation](004-public-read-only-service-isolation.md) | The prior example of a security decision that reasons explicitly about the public-repo cost |
| `stpa` skill (`.claude/skills/stpa/`) | Source of the judgment-and-mechanism (JSON extraction plus jq render) pattern considered and deferred in this ADR |
| `gh issue list --label security-finding` | The live query the index ranks against |
