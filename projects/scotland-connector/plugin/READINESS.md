# Repository readiness audit

The existing Claude package passes the supported client validator. The
portable manifest and MCP configuration pass their published JSON schemas.
OpenAI extension metadata was checked against its documented contract;
directory acceptance remains unassessed. Public wording has source gaps
listed below. This report leaves that wording unchanged for Joe.

This is the repository-only scope of #7687, split from #7255. Publication,
publisher verification, installation against production, endpoint availability
and live privacy enforcement remain with #7255. None is acceptance for this
audit. No live service probes, account changes or submissions were performed.
No new domain decision or packaging redesign is proposed.

## Evidence baseline

Audited on **2026-11-09**, Linux, at
`764ca308f021ac464a562e5597c19261607a6072`. File/line references below are
relative to this repository at that commit. The historical leads were read:

- `32fe4e8d46585289725a2760859c631524566cfb` introduced the packages and policy
  pages. Its claimed client installation is historical evidence only.
- `740b7d47802f39a044cbe893654275e794705471` moved the marketplace to the public
  route and selected the monorepo subdirectory. Its dev-server connection
  claim does not establish current production delivery.
- `../ARCHITECTURE.md:635` remains the Listings decision. The existing assets
  are reused, including `LICENSE`, icons and the shared skill.

## Client validation

All remote documents below were retrieved without authentication on
2026-11-09. SHA-256 values identify the retrieved bytes, not an assertion
that a mutable documentation URL will retain them.

| Contract | Exact source | Version or SHA-256 |
| --- | --- | --- |
| Portable manifest | https://agent-plugins.org/schemas/1.0.0/plugin.schema.json | 1.0.0; `0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883` |
| Portable MCP config | https://agent-plugins.org/schemas/1.0.0/mcp.schema.json | 1.0.0; `6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb` |
| Portable semantic and layout rules | https://agent-plugins.org/specification.md | 1.0.0; `34710a33979953fd884e9b7d69af12a39e3e1e93f465fea2afaa886a5ca82ba0` |
| OpenAI package layout and extension | https://developers.openai.com/plugins/build/plugins.md | `b913de64a45a90f14fba0d4b0654444f1f60f4e2d4f79f3dd41332895f79721d` |
| OpenAI listing fields and submission rules | https://developers.openai.com/plugins/deploy/submission.md | `484e56805448ab8c1154998ded4914ec8c3155cda04e7f8bbc876104bd64c278` |
| Claude manifest and MCP validator contract | https://code.claude.com/docs/en/plugins-reference.md | `b0f8e2f9c1e5c05b3fd975f15a6c85fb9bae15cc81b07b90137b065af09b7fce` |
| Claude marketplace sources | https://code.claude.com/docs/en/plugin-marketplaces.md | `09ca4f45324f12bfae726823b14c7b3c5bdd8f31261afe74c93691d2d21b4e1c` |
| Claude directory boundary | https://code.claude.com/docs/en/plugins/publish.md | `f98d37d7355d95ddd027676974b26886522feab56667b42cd0772f5d8f19bab5` |

### Per-package outcomes

| Package or layer | Check and outcome | Remaining limitation |
| --- | --- | --- |
| Claude `.claude-plugin/plugin.json` with `.mcp.json` | **PASS**, `claude plugin validate --strict projects/scotland-connector/plugin`, Claude Code **2.1.284**, prints `Validation passed` with no warnings. The documented validator checks MCP entries from 2.1.281 onward. | Validates local files. No production connection or directory review. Directory fields are accepted metadata, not proof that their promises hold. |
| OpenAI root `plugin.json` | **PASS**, Python `jsonschema` **4.26.0**, `Draft202012Validator`, published 1.0.0 schema, including schema self-check. | The root schema only requires each `extensions` value to be an object. It assigns no semantics to `com.openai.interface`. |
| OpenAI root `mcp.json` | **PASS**, same validator and published MCP schema. One `streamable-http` server, HTTPS URL, no headers or credentials. | URL meaning and runtime interoperability are outside JSON Schema. No network call was made. |
| OpenAI `extensions.com.openai.interface` | Documented-field inspection: string metadata, required display text, four HTTPS listing URLs, three bounded unique prompts, and packaged relative icon paths match the submission field table. Existing field-length tests pass. | No standalone authoritative extension schema or public offline extension validator was identified in the consulted sources. Installed `codex-cli 0.146.0` exposes no `plugin validate` command. Category `Travel` has not been checked against the dashboard's allowed titles. This is a contract inspection, not full client validity or approval. |
| Packaging layout | Root portable manifest/config, `skills/scotland-trips/SKILL.md`, `.claude-plugin/plugin.json`, `.mcp.json`, both icons and licence exist. Referenced asset paths stay within the package. No hooks, executables or credentials are declared. | No ZIP upload, installed-client discovery or public-directory import was attempted. Different client layouts and submission rules must stay distinct. |

`plugin_test.py` checks parity, local field lengths, PNG dimensions and skill
frontmatter. Its test names about directory acceptance and live URLs exceed
what those assertions establish. JSON parsing, a `$schema` string and these
tests do not substitute for the validator outcomes above.

The inspected display/subtitle/description/publisher lengths are 14/27/735/12
characters. Prompt lengths are 56/62/54, with three distinct strings. Both
icons are 1024 by 1024 PNGs (32,239 and 23,753 bytes). These are local asset
facts, independent of any directory's acceptance criteria.

Reproduce from the repository root, using an isolated Python environment:

```sh
python3 -m venv /tmp/scotland-audit-venv
/tmp/scotland-audit-venv/bin/pip install jsonschema==4.26.0 pytest==9.2.0
curl -fLsS https://agent-plugins.org/schemas/1.0.0/plugin.schema.json -o /tmp/scotland-plugin.schema.json
curl -fLsS https://agent-plugins.org/schemas/1.0.0/mcp.schema.json -o /tmp/scotland-mcp.schema.json
sha256sum /tmp/scotland-plugin.schema.json /tmp/scotland-mcp.schema.json
claude --version
claude plugin validate --strict projects/scotland-connector/plugin
codex --version
codex plugin --help
/tmp/scotland-audit-venv/bin/python - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator

root = Path("projects/scotland-connector/plugin")
for kind in ("plugin", "mcp"):
    schema = json.loads(Path(f"/tmp/scotland-{kind}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads((root / f"{kind}.json").read_text()))
    print(f"{kind}.json: PASS Draft 2020-12")
PY
```

These are manual audit commands. No network-dependent CI test was added.

## Skill and tool behavior

Connector source paths in this table start at `projects/scotland-connector/`.

| Source evidence | Verified behavior or gap |
| --- | --- |
| `plugin/skills/scotland-trips/SKILL.md:17`; `internal/connector/plan.go:139`; `internal/connector/check.go:91` | Open questions use `plan`; named walks use `check`, accepting a name or prior ID. Check requires activity/date, and stargazing requires coordinates. The skill's examples and weather/filter parameter names match the implementation. |
| `internal/connector/connector.go:28`; `internal/connector/plan.go:139`; `internal/connector/check.go:91` | Caps are seven days, final date no later than today plus 13, ten check items, 1 to 100 km search radius, default three options and maximum five. Dates use Europe/London. Before 09:00 the current stargazing night can start yesterday, including in a default combined plan. The skill's “today and the next two days” omits that exception; its “starting up to 13 days ahead” must not imply another seven days beyond the final-date cap. |
| `internal/walks/walks.go:537`; `internal/connector/trip.go:253` | Default good-hour limits are 2 mm/h rain and 80 km/h wind. Suggested overrides of 0 or 4 mm/h, 40 km/h and 0 degrees C are accepted. Looser limits need a complete snapshot (`needsComplete`); the skill omits that possible error. A marginal slot can include a bad hour, so “dry” is not a guarantee of an entirely dry marginal outing. |
| `internal/connector/trip.go:20`; `internal/walks/verdict.go:90`; `internal/connector/connector_test.go` | Start-point weather, summit caveat, MWIS advice, route links and climatology distinction are supported. `unknown` can mean missing entity forecast metadata or incomplete supporting coverage, not only a date beyond roughly 2.5 days. Read the returned `why`; the skill and support copy over-narrow this meaning. |
| `internal/connector/light.go:42`; `internal/connector/light.go:74`; `internal/mcp/tools.go:36` | The skill says `light` “has no verdict”. Current output has cloud-support verdicts and separate night suitability verdicts. Proposed correction: distinguish astronomical windows, partial cloud support and night suitability. |
| `internal/mcp/mcp.go:91`; `internal/mcp/tools.go:42`; `deploy/values.yaml:12` | Aurora is advertised only with an enabled store. Repository deploy values enable it, while chart defaults disable it; neither proves live state. The skill correctly says “when it is listed” but supplies no exact-instant, stale/missing-input or heuristic guidance. Proposed addition: aurora supports the published NOAA instant only, with no multi-day forecast or calibrated sighting probability. |
| `internal/mcp/tools.go:27`; `internal/mcp/tools.go:126`; `internal/connector/plan.go:406`; `internal/connector/aurora.go:17` | All four tools have descriptive titles and matching annotation titles. All use `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`. Calls read snapshots and compute results; they do not fetch providers or mutate user/domain data. Metrics and internal-error logging are incidental effects. Clocks and refreshed snapshots can change answers; idempotence does not promise byte-identical replies. |
| `internal/mcp/mcp_test.go:102` | `TestToolListAndAnnotations` asserts titles/read-only/destructive hints; the other two booleans were inspected in the common constructor. Existing tests do not independently assert idempotent/open-world values. |

## Public pages and claims

Public source was read after `projects/monolith/AGENTS.md`, its README tier
boundary and `docs/runbooks/public-tier-checklist.md`. This audit changes no
route, grant, caching policy or deployment. References in this section are
relative to `projects/monolith/` unless stated otherwise.

- **Marketplace and naming.** `frontend/src/routes/public/scotland/scotland-data.js:15`
  names marketplace `jomcgi`, plugin `scotland`, and the public Git repository's
  `projects/scotland-connector/plugin` subdirectory on `main`. Both manifests
  use `scotland`, version `0.1.1`, Joe McGinley and the same repository URL.
  Human labels vary between “Scotland trips” and “Scotland connector”; these
  are presentation labels, not conflicting IDs. Null directory links render
  as “soon”. `marketplace.json/+server.js:10` returns the catalog with explicit
  JSON content type and one-hour shared/Cloudflare caching. Its two tests
  verify those headers and the source reference. No current clone-size or
  installation result is established: the README's “does not clone the
  monorepo” should be qualified as subdirectory packaging, subject to client
  fetching behavior. No new marketplace was created.
- **Rendering and support.** `frontend/src/routes/public/scotland/PolicyPage.svelte:13`
  renders supplied sections as text, anchored headings, dated metadata and
  policy cross-links. The privacy, terms and support wrappers each select
  the matching data object. `policy-data.js:11` uses `joe@jomcgi.dev`, matching
  manifest author and marketplace owner. `page.dom.test.js`,
  `policy.dom.test.js`, sitemap and llms tests verify rendered links and copy.
  They cannot verify mailbox delivery, replies “usually within a few days”,
  legal sufficiency or URL availability. The loading promise “clears within
  a minute or two” is not guaranteed by a 60-second snapshot refresh: fetch
  failures or missing snapshots can persist. Proposed correction: make
  recovery conditional.
- **Limits.** `frontend/src/routes/public/scotland/scotland-data.js:39`
  advertises plan/check and the supported seven-day/ten-item caps. “Tools:
  plan, check” omits `light` and conditional `aurora`. Forecast refresh every
  two to three hours matches `chart/values.yaml:662` and `:685` schedules.
  A schedule proves intent, not current data freshness. No sign-up/key is
  declared in the client packages. Connector `chart/values.yaml:170` sets
  local per-client and known-assistant buckets; its comments specify
  route/replica scope. `chart/templates/httproute.yaml` renders them. Do not
  turn these into a universal global request quota. Public terms promise
  refusal over limits but quote no numerical quota.
- **Logging and retention.** `frontend/src/routes/public/scotland/policy-data.js:37`
  describes hosted/local callers; `:49` promises rolling gateway logs, no
  bodies and no onward shipping. Connector `internal/mcp/mcp.go:433` logs
  internal tool errors, and `internal/meter/meter.go:91` records bounded
  tool/surface/client/outcome labels without coordinates or addresses.
  Protocol `_meta` is read for negotiation (`mcp.go:220`); location/user/
  conversation hints are not consumed by tool dispatch. Gateway configuration
  does not override default access logging
  (`projects/platform/cloudflare-gateway/templates/gateway-envoyproxy.yaml:8`).
  Envoy Gateway **v1.8.3** default fields include address, user agent and full
  request path, with no body field: https://raw.githubusercontent.com/envoyproxy/gateway/v1.8.3/internal/xds/translator/accesslog.go
  (retrieved 2026-11-09; SHA-256
  `a06df2485770b5a678758290bf8298d5ad77ae8813016175d8226f1e61f457cf`).
  The repository collector's opt-in namespaces exclude this gateway and
  connector (`projects/platform/otel-collector/values-gke.yaml:85`;
  `templates/logs-configmap.yaml:29`). That does not prove absence of GKE or
  Cloudflare logging, copies, backups or a retention duration. No bounded
  retention configuration was established here. Proposed correction: qualify
  “never shipped”, “kept anywhere else”, “only short-lived” and deletion
  promises until the publisher has operational evidence. User agent is both
  mentioned among ChatGPT hints and logged at the gateway; “none is kept”
  needs the same qualification. No live configuration was read.
- **Attribution and licence.** `frontend/src/routes/public/scotland/scotland-data.js:82`
  and terms list MET Norway, CERRA, OSM, Natural Earth and Walkhighlands,
  matching connector `internal/connector/connector.go:169`. Package `LICENSE`
  is byte-identical to root MPL-2.0
  (`sha256sum LICENSE projects/scotland-connector/plugin/LICENSE`). This does
  not license upstream datasets. The connector source explicitly leaves the
  light-pollution render unattributed pending #7253. Walkhighlands rights
  remain separate (#7252). Aurora adds NOAA and GMTED2010 provenance in
  `internal/connector/aurora.go:35`, absent from the page's source list.
  Proposed correction: reconcile the public list and unresolved rights before
  publication. No rights clearance or legal decision is claimed.

## Reproduce the repository tests

`plugin/BUILD:5` declares `plugin_test`; `internal/mcp/BUILD:18` declares
`mcp_test`. Frontend `BUILD:319` declares `test` with `vitest run` and the
test source closure. `vitest.config.js` includes `src/**/*.test.js`, node by
default, with happy-dom selected by the DOM test files and Svelte aliases.
The following commands ran on Linux; output was retained unfiltered.

```sh
/tmp/scotland-audit-venv/bin/pytest -v projects/scotland-connector/plugin/plugin_test.py
go test -v ./projects/scotland-connector/internal/mcp ./projects/scotland-connector/internal/connector
pnpm install --frozen-lockfile
cd projects/monolith/frontend
pnpm exec vitest run src/routes/public/scotland src/routes/public/sitemap.xml/server.test.js src/routes/public/llms.txt/server.test.js --reporter=verbose
```

- Pytest **9.2.0**: all **6 PASSED**, including manifest/config parity, listing
  field lengths, skill frontmatter and square PNG assets. These assertions
  cover repository contracts only.
- Go **1.26.10**: MCP **12 top-level tests PASS**, including
  `TestAuroraListedOnlyWhenEnabled`, `TestToolListAndAnnotations`,
  `TestToolSchemaCapsMatchCode`, `TestPlanToolCall`, `TestCheckToolCall`,
  `TestLightToolCall`, `TestLegacyClient` and `TestTransportGuards` (cached
  on the retained rerun). Connector **45 top-level tests PASS**, including
  `TestPlanValidation`, `TestCheckValidation`, `TestWeatherLimits`,
  `TestLoosenedLimitsNeedTheFullData`, `TestEntityFreshnessAcrossCheckAndCombinedPlan`
  and the light scope/expiry cases.
- Vitest **4.1.11**, pnpm **10.34.5**: **5 files, 24 tests passed**. Policy DOM
  **13**, landing DOM **4**, marketplace **2**, llms **3**, sitemap **2**.
  Tests exercise source/rendered fixtures; they make no live-delivery claim.

`ci` is not on this guest's PATH. Complete-mode runs of its repository helper
(`CI_REQUIRE_COMPLETE=1 bash bazel/tools/ci/ci lint`, then `regen`, then
`lint`) passed. Regeneration produced no tracked drift. The attempt to run
the Bazel targets through that helper failed explicitly because `bb` is
absent:

```sh
bash bazel/tools/ci/ci test -- //projects/scotland-connector/plugin:plugin_test //projects/scotland-connector/internal/mcp:mcp_test //projects/monolith/frontend:test
```

This is a failed local remote-runner invocation, not a passing Bazel test.
Required PR Linux `pr-checks` remains a separate delivery gate. The direct
Linux test results above are not substituted for that gate. The factory's
pre-push hook also repeats complete lint/regen/lint on the outgoing commit.

## External readiness not assessed

#7255 retains publisher identity and plan eligibility, directory-specific
category/policy requirements, final public wording, ZIP import and scans,
review materials, provider rights, support ownership and production behavior.
No claim is made about account eligibility, directory approval or live
availability. The audit's findings can inform that work without changing
this receipt's repository-only acceptance.
