# Repository readiness audit

The existing Claude package passes the supported client validator. The
portable manifest and MCP configuration pass their published JSON schemas.
OpenAI extension metadata was checked against its documented contract;
directory acceptance remains unassessed. Public wording has source gaps
listed below. This report leaves that wording unchanged for Joe.

This is the repository-only scope of #7687, split from #7255. Publication,
publisher verification, installation against production, endpoint availability
and live privacy enforcement belong to the separate operational child #7688,
which is open with `needs-human`. Parent #7255 is closed as not planned after
the operator split. Those checks remain outside this audit's acceptance.
No live service probes, account changes or submissions were performed.
This report stays outside `plugin/`; the distributed package is unchanged.
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
- `ARCHITECTURE.md:635` remains the Listings decision. The existing assets
  are reused, including `LICENSE`, icons and the shared skill.

Review corrections were checked on 2026-11-09 against PR #7690 head
`673daf2e4c6e2f53ace3553753c4a25c5886377c`; the implementation sources are
unchanged from the baseline. GitHub reads confirmed #7688 open with
`needs-human`, #7255 closed as not planned, #7252 completed and #7253 open.
Round 2 checks on 2026-11-10 against head
`4e886aeb419cf1b2918c0a25e0056db24a44005a` add the affiliation and
bulk-extraction assessments and explicit outcomes below. They change no
implementation, package or public wording.

## Client validation

All remote documents below were retrieved without authentication on
2026-11-09. SHA-256 values identify the retrieved bytes. Mutable documentation
URLs may change later.

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
| Claude `.claude-plugin/plugin.json` with `.mcp.json` | **PASS**, `claude plugin validate --strict projects/scotland-connector/plugin`, Claude Code **2.1.284**, prints `Validation passed` with no warnings. The documented validator checks MCP entries from 2.1.281 onward. | Validates local files. No production connection or directory review. Directory metadata still requires evidence for its promises. |
| Rendered Claude marketplace source | **PASS**, render `scotland-data.js` with the repository's `PUBLIC_BASE` into `.claude-plugin/marketplace.json`, then run `claude plugin validate --strict /tmp/scotland-review-validation/marketplace`, Claude Code **2.1.284**, exit 0, `Validation passed`, no warnings. Rechecked on 2026-11-09 against the marketplace contract above; its digest is unchanged. | Checks the catalog fields and `git-subdir` source shape locally. Remote repository/path fetching happens at installation; no client installation or live response was checked. |
| OpenAI root `plugin.json` | **PASS**, Python `jsonschema` **4.26.0**, `Draft202012Validator`, published 1.0.0 schema, including schema self-check. | The root schema only requires each `extensions` value to be an object. It assigns no semantics to `com.openai.interface`. |
| OpenAI root `mcp.json` | **PASS**, same validator and published MCP schema. One `streamable-http` server, HTTPS URL, no headers or credentials. | URL meaning and runtime interoperability are outside JSON Schema. No network call was made. |
| OpenAI `extensions.com.openai.interface` | Documented-field inspection: string metadata, required display text, four HTTPS listing URLs, three bounded unique prompts, and packaged relative icon paths match the submission field table. Existing field-length tests pass. | No standalone authoritative extension schema or public offline extension validator was identified in the consulted sources. Installed `codex-cli 0.146.0` exposes no `plugin validate` command. Category `Travel` has not been checked against the dashboard's allowed titles. Full client validity and approval remain unassessed. |
| Packaging layout | Root portable manifest/config, `skills/scotland-trips/SKILL.md`, `.claude-plugin/plugin.json`, `.mcp.json`, both icons and licence exist. Referenced asset paths stay within the package. No hooks, executables or credentials are declared. This audit is outside `plugin/` and linked from the domain architecture; the shipped README and package files are unchanged. | No ZIP upload, installed-client discovery or public-directory import was attempted. Different client layouts and submission rules must stay distinct. |

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

### Marketplace validator output

The catalog is rendered from the existing module; no marketplace file is
added to the distributed plugin. From the repository root:

```sh
node --input-type=module <<'JS'
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const seoURL = pathToFileURL(resolve('projects/monolith/frontend/src/lib/public/seo.js'));
const { PUBLIC_BASE } = await import(seoURL.href);
const source = await readFile('projects/monolith/frontend/src/routes/public/scotland/scotland-data.js', 'utf8');
const renderedSource = source.replace('import { PUBLIC_BASE } from "$lib/public/seo.js";', `const PUBLIC_BASE = ${JSON.stringify(PUBLIC_BASE)};`);
if (renderedSource === source) throw new Error('PUBLIC_BASE import did not match');
const { marketplace } = await import(`data:text/javascript;base64,${Buffer.from(renderedSource).toString('base64')}`);
const target = '/tmp/scotland-review-validation/marketplace/.claude-plugin';
await mkdir(target, { recursive: true });
await writeFile(`${target}/marketplace.json`, `${JSON.stringify(marketplace, null, 2)}\n`);
JS
claude --version
claude plugin validate --strict /tmp/scotland-review-validation/marketplace
sha256sum /tmp/scotland-review-validation/marketplace/.claude-plugin/marketplace.json
```

Recorded output, exit 0:

```text
2.1.284 (Claude Code)
Validating marketplace manifest: /tmp/scotland-review-validation/marketplace/.claude-plugin/marketplace.json

✔ Validation passed
3f040686ffd5ba5048c63dccda892571a3a68a0e252334e8bffed4fd418cefc6  /tmp/scotland-review-validation/marketplace/.claude-plugin/marketplace.json
```

## Skill and tool behavior

Connector source paths in this table start at `projects/scotland-connector/`.
**PASS** means the named check is supported by repository evidence;
**FAIL** identifies a source or wording gap; **UNVERIFIED** requires evidence
outside this audit. Mixed outcomes name the separate checks.

| Source evidence | Verified behavior or gap |
| --- | --- |
| `plugin/skills/scotland-trips/SKILL.md:17`; `internal/connector/plan.go:139`; `internal/connector/check.go:91` | **PASS (tool selection and parameters).** Open questions use `plan`; named walks use `check`, accepting a name or prior ID. Check requires activity/date, and stargazing requires coordinates. The skill's examples and weather/filter parameter names match the implementation. |
| `internal/connector/connector.go:28`; `internal/connector/plan.go:139`; `internal/connector/check.go:91`; `internal/connector/light.go:74` | **PASS (implemented caps); FAIL (skill date wording).** Plan spans at most seven days, with its final date no later than today plus 13. Plan has a 1 to 100 km search radius and returns three options by default, at most five. Light shares the radius cap. Check accepts at most ten items within the date horizon and has no radius or option-count parameter. Dates use Europe/London. Before 09:00 the current stargazing night can start yesterday, including in a default combined plan. The skill's “today and the next two days” omits that exception; its “starting up to 13 days ahead” must not imply another seven days beyond the final-date cap. |
| `internal/walks/walks.go:537`; `internal/connector/trip.go:253` | **PASS (weather parameters); FAIL (skill limitations).** Default good-hour limits are 2 mm/h rain and 80 km/h wind. Suggested overrides of 0 or 4 mm/h, 40 km/h and 0 degrees C are accepted. Looser limits need a complete snapshot (`needsComplete`); the skill omits that possible error. A marginal slot can include a bad hour, so “dry” is not a guarantee of an entirely dry marginal outing. |
| `internal/connector/trip.go:20`; `internal/walks/verdict.go:90`; `internal/connector/connector_test.go` | **PASS (weather and source caveats); FAIL (`unknown` wording).** Start-point weather, summit caveat, MWIS advice, route links and climatology distinction are supported. `unknown` can mean missing entity forecast metadata, incomplete supporting coverage or a date beyond roughly 2.5 days. Read the returned `why`; the skill and support copy over-narrow this meaning. |
| `internal/connector/light.go:42`; `internal/connector/light.go:74`; `internal/mcp/tools.go:36` | **FAIL (light verdict wording).** The skill says `light` “has no verdict”. Current output has cloud-support verdicts and separate night suitability verdicts. Proposed correction: distinguish astronomical windows, partial cloud support and night suitability. |
| `internal/mcp/mcp.go:94`; `internal/mcp/tools.go:42`; `deploy/values.yaml:12` | **PASS (conditional listing); FAIL (missing skill guidance); UNVERIFIED (live enablement, #7688).** Aurora is advertised only with an enabled store. Repository deploy values enable it, while chart defaults disable it; neither proves live state. The skill correctly says “when it is listed” but supplies no exact-instant, stale/missing-input or heuristic guidance. Proposed addition: aurora supports the published NOAA instant only, with no multi-day forecast or calibrated sighting probability. |
| `internal/mcp/tools.go:27`; `internal/mcp/tools.go:126`; `internal/connector/plan.go:406`; `internal/connector/aurora.go:17` | **PASS (titles and annotations).** All four tools have descriptive titles and matching annotation titles. All use `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`. Calls read snapshots and compute results; they do not fetch providers or mutate user/domain data. Metrics and internal-error logging are incidental effects. Clocks and refreshed snapshots can change answers; idempotence does not promise byte-identical replies. |
| `internal/mcp/mcp_test.go:102` | **PASS (existing assertion coverage).** `TestToolListAndAnnotations` asserts titles/read-only/destructive hints; the other two booleans were inspected in the common constructor. Existing tests do not independently assert idempotent/open-world values. |

## Public pages and claims

Public source was read after `projects/monolith/AGENTS.md`, its README tier
boundary and `docs/runbooks/public-tier-checklist.md`. This audit changes no
route, grant, caching policy or deployment. References in this section are
relative to `projects/monolith/` unless stated otherwise.

- **Marketplace and naming: PASS (source metadata and cache configuration).**
  `frontend/src/routes/public/scotland/scotland-data.js:15`
  names marketplace `jomcgi`, plugin `scotland`, and the public Git repository's
  `projects/scotland-connector/plugin` subdirectory on `main`. Both manifests
  use `scotland`, version `0.1.1`, Joe McGinley and the same repository URL.
  Human labels vary between “Scotland trips” and “Scotland connector”; both
  use the same plugin ID. Null directory links render
  as “soon”. `marketplace.json/+server.js:10` returns the catalog with explicit
  JSON content type. `frontend/src/lib/cache-headers.js:161` sets browser
  `max-age=0`, shared `s-maxage=3600`, `stale-while-revalidate=86400` and
  `stale-if-error=31536000`. The Cloudflare header uses `max-age=3600`
  with the same stale windows. Its two tests verify those headers and the
  source reference; the strict catalog validator result is recorded above.
  **UNVERIFIED (fetching, installation and live caching, #7688).**
  No current clone-size or installation result is established: the README's
  “does not clone the monorepo” should be qualified as subdirectory packaging,
  subject to client
  fetching behavior. No new marketplace was created.
- **Implied affiliation: PASS (publisher identified); UNVERIFIED (implied
  affiliation).** `frontend/src/routes/public/scotland/policy-data.js:32`
  is the only individual-publisher statement: “run by Joe McGinley, an
  individual in Scotland”. The plugin manifests name Joe McGinley as author,
  and the OpenAI interface names him as developer. No inspected public page,
  manifest or listing explicitly disclaims official or government
  affiliation. The labels and author fields alone do not establish that the
  name cannot be mistaken for an official body. Directory-review risk for
  the bare `scotland` ID remains **UNVERIFIED under #7688**, including its
  publisher identity and namespace review. Proposed action for Joe: review
  an explicit non-affiliation statement alongside the public name before
  publication. This audit supplies no approved disclaimer and changes no copy.
- **Rendering and support: PASS (rendering and contact consistency).**
  `frontend/src/routes/public/scotland/PolicyPage.svelte:13`
  renders supplied sections as text, anchored headings, dated metadata and
  policy cross-links. The privacy, terms and support wrappers each select
  the matching data object. `policy-data.js:11` uses `joe@jomcgi.dev`, matching
  manifest author and marketplace owner. `page.dom.test.js`,
  `policy.dom.test.js`, sitemap and llms tests verify rendered links and copy.
  **UNVERIFIED (mailbox delivery, reply time, legal sufficiency and live URL
  availability, #7688).** These tests cannot verify replies “usually within
  a few days”. **FAIL (recovery-time wording).** The loading promise “clears
  within a minute or two” is not guaranteed by a 60-second snapshot refresh: fetch
  failures or missing snapshots can persist. Connector
  `internal/connector/trip.go:23` instead says “retry in a few minutes”.
  Proposed correction: align both messages and make recovery conditional.
- **Limits: PASS (source caps).**
  `frontend/src/routes/public/scotland/scotland-data.js:39`
  advertises plan/check and the supported seven-day/ten-item caps.
  **FAIL (tool inventory).** “Tools: plan, check” omits `light` and conditional
  `aurora`. **PASS (refresh schedules).** Forecast refresh every
  two to three hours matches `chart/values.yaml:662` and `:685` schedules.
  **UNVERIFIED (current data freshness, #7688).** A schedule supplies no live
  freshness evidence. **PASS (package declarations and rate-limit source).** No
  sign-up/key is declared in the client packages. Connector
  `chart/values.yaml:170` sets local per-client and known-assistant buckets;
  its comments specify
  route/replica scope. `chart/templates/httproute.yaml` renders them. Do not
  turn these into a universal global request quota. Public terms promise
  refusal over limits but quote no numerical quota; live enforcement remains
  **UNVERIFIED under #7688**.
- **Bulk extraction: PASS (contractual restriction and per-call bounds);
  UNVERIFIED (live enforcement, #7688).**
  `frontend/src/routes/public/scotland/policy-data.js:106` forbids bulk
  extraction, rebuilding or redistribution of the underlying data, and
  getting round rate limits. The following paths start at
  `projects/scotland-connector/`. `internal/connector/connector.go:25`
  declares caps and no corpus-listing operation. `internal/connector/plan.go:139`
  bounds each plan to seven days, a 100 km radius and at most five options;
  `internal/connector/check.go:91` bounds each check to ten items.
  `chart/values.yaml:158-191` configures local per-client and known-assistant
  rate buckets, scoped by route and Envoy replica. These source controls
  bound individual requests and request rates. The bulk-extraction
  prohibition is contractual: many in-limit calls can still sweep the grid.
  Source limits do not establish prevention of bulk reconstruction or
  redistribution. Enforcement in the live service remains unverified; no
  extraction or rate-limit probe was run.
- **Logging and retention: UNVERIFIED (privacy promises, #7688).**
  `frontend/src/routes/public/scotland/policy-data.js:37`
  describes hosted/local callers; `:49` promises rolling gateway logs, no
  bodies and no onward shipping. **PASS (inspected source and default format).**
  Connector `internal/mcp/mcp.go:433` logs
  internal tool errors, and `internal/meter/meter.go:91` records bounded
  tool/surface/client/outcome labels without coordinates or addresses.
  Protocol `_meta` is read for negotiation (`mcp.go:228`); location/user/
  conversation hints are not consumed by tool dispatch. Gateway configuration
  does not override default access logging
  (`projects/platform/cloudflare-gateway/templates/gateway-envoyproxy.yaml:8`).
  Envoy Gateway **v1.8.3** default fields include address, user agent and full
  request path, with no body field: https://raw.githubusercontent.com/envoyproxy/gateway/v1.8.3/internal/xds/translator/accesslog.go
  (retrieved 2026-11-09; SHA-256
  `a06df2485770b5a678758290bf8298d5ad77ae8813016175d8226f1e61f457cf`).
  The repository collector's opt-in namespaces exclude this gateway and
  connector (`projects/platform/otel-collector/values-gke.yaml:85`;
  `templates/logs-configmap.yaml:29`). **UNVERIFIED (actual logging, onward
  copies and retention).** That does not prove absence of GKE or
  Cloudflare logging, copies, backups or a retention duration. No bounded
  retention configuration was established here. Proposed correction: qualify
  “never shipped”, “kept anywhere else”, “only short-lived” and deletion
  promises until the publisher has operational evidence. User agent is both
  mentioned among ChatGPT hints and logged at the gateway; “none is kept”
  needs the same qualification. No live configuration was read.
  `frontend/src/routes/public/scotland/policy.dom.test.js:90` asserts
  “never shipped”; any later approved wording correction must update that
  assertion with the policy copy.
- **Attribution and licence: PASS (listed source identities and package licence);
  FAIL (missing source attribution).**
  `frontend/src/routes/public/scotland/scotland-data.js:82`
  and terms list MET Norway, CERRA, OSM, Natural Earth and Walkhighlands,
  matching connector `internal/connector/connector.go:169`. Package `LICENSE`
  is byte-identical to root MPL-2.0
  (`sha256sum LICENSE projects/scotland-connector/plugin/LICENSE`). This does
  not license upstream datasets. **FAIL (light-pollution attribution, #7253).**
  The connector source explicitly leaves that render unattributed pending
  open #7253. **PASS (recorded Walkhighlands sourcing boundary).** Walkhighlands
  sourcing was decided in completed #7252 and recorded in the connector's
  `ARCHITECTURE.md:454`: facts and links, with route prose, maps and GPX
  excluded. **FAIL (public NOAA/GMTED2010 attribution).** Aurora adds their
  provenance in `internal/connector/aurora.go:35`, absent from the page's source list.
  Proposed correction: resolve the #7253 attribution gap and add the missing
  NOAA/GMTED2010 attribution to the public list before publication. This audit
  makes no new rights-clearance or legal decision; provider rights remain
  **UNVERIFIED under #7688**.

### Query and body exposure

The public API prefix is `/scotland`. Route paths below are connector-local
after that prefix is stripped. Source references start at
`projects/scotland-connector/`. The exposure column follows the Envoy Gateway
v1.8.3 default request-path field inspected above; live logging and retention
remain unverified under #7688.

| Route or transport | Argument source | Exposure in the inspected default access log (PASS: source mapping only) |
| --- | --- | --- |
| `GET /v1/plan` | `internal/httpapi/httpapi.go:134-157`: query `lat`, `lon`, `from`, `to`, `radius_km`, activities, option count and weather/walk filters. | The full query string includes supplied coordinates, dates and filters. |
| `GET /v1/light` | `internal/httpapi/httpapi.go:174-188`: query `lat`, `lon`, `radius_km`, `from`, `to`. | The full query string includes supplied coordinates, radius and dates. |
| `GET /v1/aurora` | `internal/httpapi/aurora.go:10-25`: query `lat`, `lon`, optional `at`. | The full query string includes supplied coordinates and the requested instant. |
| `POST /v1/check` | `internal/httpapi/httpapi.go:201-214`; `internal/connector/check.go:15-32`: JSON body with item dates, names/IDs and stargazing coordinates, plus shared weather limits. | Path/query are logged. The body arguments are outside the default format's fields. |
| `POST /mcp` (`plan`, `check`, `light`, `aurora`) | `internal/mcp/mcp.go:158-184`, `:401-411`: JSON-RPC body with `params.arguments`. | Path/query are logged. Tool arguments in the body are outside the default format's fields. |

**FAIL (privacy query-transport coverage).**
Privacy `frontend/src/routes/public/scotland/policy-data.js:43` mentions only
plan's point and dates in the URL; `:51` says “so a plan's coordinates”. Light
and aurora use the same query transport. Proposed replacement for Joe's
review: “Direct REST calls put plan and light coordinates and dates, and
aurora coordinates and any requested instant, in the URL query string. The
gateway's default access-log format includes the path and query string.
Check and MCP tool arguments travel in the request body, which that default
format omits.” This describes the inspected format. Actual logging, onward
copies and retention need #7688's operational evidence before a public
promise is approved. The current policy and its tests remain unchanged.

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

This local remote-runner invocation failed before running Bazel tests.
Required PR Linux `pr-checks` remains a separate delivery gate. The direct
Linux test results above are not substituted for that gate. The factory's
pre-push hook also repeats complete lint/regen/lint on the outgoing commit.

## External readiness not assessed

#7688, the operational child split from #7255, retains publisher identity and
plan eligibility, directory-specific category/policy requirements, final
public wording, ZIP import and scans, review materials, provider rights,
support ownership and production behavior.
Its `needs-human` hold requires explicit publisher authorization. The retained
checklist includes live docs/privacy/terms/support/marketplace responses and
edge caching, running logging and retention, live `tools/list` and bounded
plan/check requests, both client installations, exact publisher accounts and
namespace, naming and implied affiliation, duplicate-submission checks, and
review/listing outcomes.
No claim is made about account eligibility, directory approval or live
availability. The audit's findings can inform that work without changing
this receipt's repository-only acceptance.
