# Staged directory submissions

Prepared **2026-11-13** for #7688 against repository revision
`8f982cc1e298df87c18bd502576650d9257bc727`. Nothing has been submitted.
This packet stays outside `plugin/`. Joe owns final public wording.
[READINESS.md](READINESS.md) remains the earlier repository audit; its
historical labels and installation claims grant no publishing authority.

The [2026-11-12 instruction](https://github.com/jomcgi-org/homelab/issues/7688#issuecomment-6838284417)
says free listings only and prepare submissions. Exact owning accounts,
publishing identity and namespace still require approval **before any target
account access, connection, upload, validation in a portal or attestation**.
Account creation belongs to Joe. No purchases, DNS or credential changes,
submission, publication, merge or deployment are part of this preparation.
The public preflight gate cannot replace those approvals or satisfy #7688.

## Decisions to record later

Keep private account identifiers and reviewer credentials out of this public
repository. Record exact approved account references in the private receipt;
a public record can say approval exists and point to its non-secret reference.

| Target | Owning account | Proposed public identity and evidence | Exact approval still needed |
| --- | --- | --- | --- |
| Claude plugin and remote connector | Unknown; no approved claude.ai organization, plan or submitter role | Joe McGinley, from both plugin manifests and the individual-publisher statement in `policy-data.js` | Which organization owns both listings, eligible existing plan and submitter, GitHub connection, final name/slug and wording; no purchase |
| OpenAI plugin directory | Unknown; no approved organization, project or verified developer identity | Joe McGinley, from `plugin.json` author and `extensions.com.openai.interface.developerName` | Exact organization/project and verified identity, authorized submitter, final category and wording, any domain challenge |
| MCP Registry | Unknown; no approved registry principal or namespace | Joe McGinley as candidate publisher, same source; repository is `jomcgi-org/homelab` | Exact namespace, authorized principal and verification route, metadata wording/version |

Namespace candidates, **none approved or claimed**:

| Candidate | Authorization method | Missing evidence |
| --- | --- | --- |
| `io.github.jomcgi/scotland` | GitHub OAuth as that exact user | Approval to use that personal identity and namespace; public issue author is only a lead |
| `io.github.jomcgi-org/scotland` | GitHub organization Owner proof | Repository ownership alone grants no namespace right. Current registry docs say interactive GitHub App device OAuth cannot discover org memberships; org publication needs approved PAT or Actions OIDC and Owner proof. Neither credential route is authorized here |
| `dev.jomcgi/scotland` | Domain verification of `jomcgi.dev` | Approval of domain identity and proof method. DNS uses an apex MCPv1 TXT public key; HTTPS uses `/.well-known/mcp-registry-auth` with the proof record. Both require signing credentials; DNS changes and proof publication remain held |

The [registry authentication contract](https://modelcontextprotocol.io/registry/authentication)
was fetched on 2026-11-13. Personal OAuth grants `io.github.<username>/*`;
domain authentication uses reverse DNS. Namespace verification does not
verify control of the remote MCP URL. Prefer an existing approved route with
no new spend; do not generate keys, log in or change infrastructure from this packet.

## Official routes

Sources below were read on **2026-11-13**, without target authentication.
Portal URLs are references only; no portal was accessed.

| Target and official source | Submission material and contract | Free-route constraint/status |
| --- | --- | --- |
| [Claude eligibility](https://claude.com/docs/directory/publish), [plugin source fields](https://claude.com/docs/plugins/submit) | `https://claude.ai/directory/manage`, Plugin bundle. Repository `jomcgi-org/homelab`, plugin path `projects/scotland-connector/plugin`, tracked branch candidate `main`. Connected GitHub user must have push access. First submitting organization holds that folder's listing | No separate listing charge identified. **Submission requires an existing paid Claude plan** (Pro, Max, Ultra, Team or Enterprise); Free cannot submit. Team requires Owner; Enterprise also permits an assigned Directory role. Eligible account unknown. Do not buy a plan |
| [Claude remote connector](https://claude.com/docs/connectors/building/submission) | Same portal, MCP connector, URL `https://jomcgi.dev/scotland/mcp`. Submit this server separately, then the plugin bundle from the same approved organization; pair later if approved | Same eligibility. Public no-auth data is supported. A connector listing contains server configuration, not the shared skill. This service has no MCP App UI; UI carousel requirements do not establish a need to invent screenshots |
| [OpenAI submission](https://developers.openai.com/plugins/deploy/submission), [package contract](https://developers.openai.com/plugins/build/plugins) | `https://platform.openai.com/plugins`, upload plugin ZIP with remote MCP included initially. Choose verified developer identity and owning organization/project. Owner or Apps Management Write required. Portable root `plugin.json`, `mcp.json`, shared skill and icons are supported | No listing fee stated in these sources; no spending authorized. Dashboard access/eligibility unknown. No-auth MCP is supported. No API key, Basic auth or nonempty headers workaround. Domain challenge, if requested, remains held |
| [MCP Registry remote-only quickstart](https://modelcontextprotocol.io/registry/quickstart) and [authentication](https://modelcontextprotocol.io/registry/authentication) | Official `https://registry.modelcontextprotocol.io`, remote-only `server.json` metadata and `mcp-publisher`; no npm package needed. Login and publishing are separate authenticated held actions | No fee stated. Registry is in preview; resets/breaking changes possible. No account creation or paid package hosting proposed |

Claude source docs were read through the public web reader. Direct Markdown
fetches for Claude returned 403 and cannot supply successful-content digests.
The [Claude checklist](https://claude.com/docs/plugins/pre-submission-checklist)
also checks the whole repository's archive size and layout, including a
50 MiB archive, 256 MiB unpacked and fewer than 10,000 entries. Current tracked
file count is 5,061; archive size and directory-specific count remain unverified.
The monorepo's subdirectory package does not prove that whole-repository gate.
Local validation does not establish name availability or directory acceptance.

## Listing drafts

All fields below are proposals for owner review. Existing shipped descriptions
have known wording gaps in READINESS.md. Do not upload them unchanged as an
attestation of accuracy. Reuse the existing icons, skill and package; this PR
changes no public copy or distributed files.

| Field | Draft or existing source |
| --- | --- |
| Plugin ID/version | `scotland`, `0.1.1`, from both manifests; no version bump for this packet |
| Public name | Scotland trips |
| Short description | Walks and stars by forecast |
| Description, one shared draft | Plan and check Scottish walks and stargazing using forecast snapshots. Get timing, route links and reasons for the returned verdicts. Weather is at the walk start; summits can differ. Missing or unsupported forecasts remain unknown. Light windows have separate cloud and night suitability evidence. Aurora, when advertised, supports one published NOAA forecast instant and is a heuristic, not a sighting probability. Source attribution and bulk-use restrictions apply. |
| Publisher/contact | Joe McGinley; existing public contact `joe@jomcgi.dev`, homepage `https://jomcgi.dev`. Account ownership and support commitments unconfirmed |
| Website / documentation | `https://jomcgi.dev/scotland`; method `https://jomcgi.dev/scotland/v1/method`; schema `https://jomcgi.dev/scotland/v1/openapi.json` |
| Privacy / terms / support | `https://jomcgi.dev/scotland/privacy`, `https://jomcgi.dev/scotland/terms`, `https://jomcgi.dev/scotland/support` |
| Source / licence | `https://github.com/jomcgi-org/homelab/tree/main/projects/scotland-connector/plugin`; MPL-2.0 for package code, no upstream-data licence grant |
| Icons | `plugin/assets/icon.png` and `icon-dark.png`, both 1024 × 1024 PNG; reuse for listing/composer |
| Keywords | scotland, walking, hiking, stargazing, weather |
| Starter prompts | Which walks near Aviemore suit the weather this weekend? / Is Ben Nevis on for Saturday, and if not, which day is better? / Where is the best stargazing near Inverness this week? |

Claude plugin source fields come from `.claude-plugin/plugin.json`; its README
becomes listing description. Any correction to that README is Joe's later
public-copy decision. For the remote connector draft: server name Scotland
trips; one-liner Walks and stars by forecast; description above; slug candidate
`scotland-trips`; category candidate Travel (confirm selectable taxonomy);
company field candidate Joe McGinley (individual), no invented company.
The connector form allows name/one-liner/description lengths 100/200/2,000,
one to five categories, docs/privacy/support/icon, use cases, company/contact,
authentication, data handling, testing and compliance. Choose public no-auth
only after confirming actual behavior. Reviewer-account fields require owner
clarification for this anonymous service; invent no account or credentials.

OpenAI uses `extensions.com.openai.interface`: reuse the existing four listing
URLs, prompts and relative `./assets/...` paths. Draft description above is
under 4,000 characters; name/subtitle/publisher limits are 30/30/80. `Travel`
is an existing candidate, not a verified dashboard category. Verified identity
determines the displayed publisher. The package declares one streamable-http
server in root `mcp.json`; a remote URL alone does not distribute the skill.

OpenAI review materials remain drafts. Five positive cases for later client
execution: (1) one-day walking plan near Aviemore, `plan`, at most one option;
(2) Ben Nevis on a current date, `check`, verdict/reason or explicit unknown;
(3) one-night Inverness stargazing check, `check`, darkness/cloud evidence;
(4) Aviemore one-day light windows, `light`, distinct astronomy/cloud support;
(5) aurora at the published instant, `aurora` only if advertised, evidence or
unknown, never sighting probability. Negative cases: a request outside Scotland
must clarify/refuse; an eight-day plan must be narrowed/rejected; bulk route
extraction must be refused. These eight cases have **not** been run in a real
client. Accessible video walkthrough URL is missing. Release-note draft:
“Initial Scotland trips package 0.1.1, remote tools plus shared trip skill.”
Reviewer credentials are unnecessary for a truly anonymous service, subject
to portal confirmation; never place private reviewer details in a package.

MCP Registry metadata candidate, pending exact namespace and version approval:

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.jomcgi/scotland",
  "description": "Plan and check Scottish walks and stargazing using forecast snapshots.",
  "repository": {
    "url": "https://github.com/jomcgi-org/homelab",
    "source": "github"
  },
  "version": "0.1.1",
  "remotes": [
    { "type": "streamable-http", "url": "https://jomcgi.dev/scotland/mcp" }
  ]
}
```

This is metadata for the server, not a plugin ZIP or shared-skill installer.
The example name is only a schema-validation candidate. Registry publication
requires namespace authorization even though the endpoint is anonymous.

## Attestations remain held

| Target | Statements the owner must settle before attesting |
| --- | --- |
| Claude | [Software Directory Terms](https://support.claude.com/en/articles/13145338-anthropic-software-directory-terms) and [Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy); accurate description/documentation, maintained security/functionality and prompt security response. Connector Compliance requires seven acknowledgments: directory guidelines, first-party API usage, financial transactions, AI media generation, prompt injection, conversation-data collection and public documentation. Data-handling form asks own API, authorized partner proxy or third-party API, health data and sponsored content; upstream rights cannot be inferred from owning connector code. Test & launch confirmation of all tools exercised is still unavailable |
| OpenAI | [Plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines): accurate identity/functionality, rights to supplied material, published privacy policy covering collection, use, recipients, retention and user controls; accurate tool hints and restricted-use compliance. Submit for review requires the six attestations shown in the official screenshot below. Reconfirm current form wording after approval and before accepting any. Five positive/three negative cases and video remain pending |
| MCP Registry | Authority over the exact namespace, accurate metadata and authorization to advertise the remote endpoint. Public schema validity proves none of these. Capture any current CLI/registry terms before agreeing; no hidden attestation wording is known here |

No checkbox has been accepted. Claude public documentation names acknowledgment
categories; its exact authenticated checkbox wording remains unavailable. The
later decision receipt must contain that wording and the owner's approval,
including any rights representation or ongoing support obligation.

The [official OpenAI submission screenshot](https://cdn.openai.com/devhub/docs/plugins/submission/submit-for-review-20260927.webp),
read on 2026-11-13, shows these exact owner statements. It is a documented
example, not a read of the current authenticated form:

1. “I have reviewed and agree to OpenAI’s Terms and Plugin Guidelines. I confirm that my plugin is in compliance with those Terms and Guidelines.”
2. “I confirm that my plugin complies with all laws and regulations applicable to the industry it operates in. I understand that I am responsible for that compliance.”
3. “I confirm that my plugin does not initiate or execute money transfers, crypto transfers, or investment trades on behalf of users.”
4. “I confirm that I have all necessary rights to any third-party content or API endpoints accessed, displayed, or used by my plugin.”
5. “I confirm that my plugin is suitable for individuals under 18 and does not include mature or adult content.”
6. “I confirm that my plugin does not target children under 13 or share their personal information with OpenAI.”

## Evidence and gaps

| Evidence class | Current observation | Status |
| --- | --- | --- |
| Source metadata | Both manifests `0.1.1`; shared skill, icons, MPL-2.0 and no credential declarations reconfirmed at baseline above | Source fact; offline checks below |
| Source tools | `internal/mcp/tools.go`: light, aurora, plan, check. All have titles, readOnly=true, destructive=false, idempotent=true, openWorld=false. `mcp.go` advertises aurora conditionally | Source fact; live inventory unavailable |
| Source limits | Plan/light: coordinates 54.5–61, -9–-0.5, radius 1–100 km, at most seven days and final date today+13; plan limit 1–5 (default 3). Check: 1–10 items, activity/date, walk ID/name or stargazing coordinates; same horizon. Weather: rain 0–10 mm/h, wind 0–120 km/h, temperature -30–30 C. MCP body cap 64 KiB | Source fact; no boundary stress requests made |
| Read-only behavior | Calls use snapshots and calculations. Meter counters and internal error logs are incidental effects. Time and snapshot refresh can change results | Source fact; edge denials cannot verify runtime effects |
| Public wording | Landing web reader shows only plan/check, directory links “soon” and Codex MCP-only config. Shared skill overstates light having no verdict, forecast-only unknown, date horizon and dry marginal slots | Wording gaps; no public-copy edit |
| Privacy/log retention | `policy-data.js` updated 2026-10-21 claims no bodies, rolling gateway logs, no onward copies and short retention. READINESS maps request path/query and user agent to gateway logs; counters/errors exist. No current retention duration, GKE/Cloudflare copy or deletion enforcement evidence | Unavailable operational evidence; do not attest “none is kept” or bounded retention |
| Rights/affiliation | Package licence does not license datasets. Walkhighlands facts/links boundary in ARCHITECTURE; source credits MET, CERRA, OSM, Natural Earth. Light-pollution rights/attribution #7253 and public NOAA/GMTED credits remain gaps | No new rights clearance. Bare Scotland name may imply affiliation; Joe must approve any disclaimer |
| Restrictions/support | Public terms source forbids bulk extraction, redistribution and rate-limit evasion. Bounded requests/rate buckets do not prove cross-request enforcement. Contact address exists in source; “within a few days” support and recovery-time promises unverified | No bulk probes or email sent; support ownership pending |
| OpenAI location policy | Guidelines advise against raw location schema fields in favor of controlled client context; actual tools require coordinates | Directory-review gap; no claim of compliant location handling and no runtime redesign in this task |
| Duplicates | Public search for `"Scotland trips" "jomcgi"` across Claude/ChatGPT/MCP domains returned no results. Registry search `https://registry.modelcontextprotocol.io/v0.1/servers?search=scotland&limit=10` returned `{"servers":[],"metadata":{"count":0}}` on 2026-11-13 | No observed public match. Pending private submissions cannot be excluded. Authenticated duplicate checks held |

### Public URL results

One unauthenticated Python urllib GET per URL on 2026-11-13. No Authorization
header, cookies or directory credentials. All final URLs equal requested
URLs; all returned HTTP **403**, body `error code: 1010\n` (17 bytes), SHA-256
`2938e9f1284180959e33ab1718d0793a72ff6e4cdb8108c34dcd14e69446de5c`.

| Requested and final URL | Expected content | Observed result |
| --- | --- | --- |
| https://jomcgi.dev/scotland | Landing and installation links | 403, unavailable via direct fetch |
| https://jomcgi.dev/scotland/v1/method | Method documentation | 403, unavailable |
| https://jomcgi.dev/scotland/privacy | Current privacy policy | 403, unavailable |
| https://jomcgi.dev/scotland/terms | Current usage restrictions | 403, unavailable |
| https://jomcgi.dev/scotland/support | Contact and support instructions | 403, unavailable |
| https://jomcgi.dev/scotland/marketplace.json | JSON catalog, scotland entry and git-subdir source | 403, unavailable |
| https://jomcgi.dev/scotland/v1/openapi.json | OpenAPI 3.1 response contract | 403, unavailable |

A public web-reader read separately retrieved landing text matching the source;
it supplies no HTTP status, deployment revision or policy-page availability.
Its privacy/terms/support reads failed. These observations do not establish
an origin outage or a deployed revision. Report edge denial for scope assessment;
changing Cloudflare rules or disguising the caller is outside this task.

### Bounded MCP probe

Exactly one tools/list, one plan and one check POST were attempted on
2026-11-13 at **07:53:49 UTC**. No retries, broad extraction or polling.
URL for all: `https://jomcgi.dev/scotland/mcp`; no redirects observed.
Headers: `Content-Type: application/json`,
`Accept: application/json, text/event-stream`,
`MCP-Protocol-Version: 2025-11-25`. The source permits stateless calls in this
legacy revision; no account or session is needed for these source contracts.

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"plan","arguments":{"lat":57.19,"lon":-3.83,"radius_km":1,"from":"2026-11-13","to":"2026-11-13","activities":["walk"],"limit":1}}}
```

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"check","arguments":{"items":[{"activity":"walk","date":"2026-11-13","walk_name":"Ben Nevis"}]}}}
```

All returned HTTP 403, 700-byte Cloudflare problem JSON rather than MCP.
Relevant response fields, with request-specific instance and ray IDs omitted:

```json
{
  "type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/",
  "title": "Error 1010: Access denied",
  "status": 403,
  "detail": "The site owner has blocked access based on your browser's signature.",
  "error_code": 1010,
  "error_name": "browser_signature_banned",
  "error_category": "access_denied",
  "timestamp": "2026-11-13T07:53:49Z",
  "zone": "jomcgi.dev",
  "cloudflare_error": true,
  "retryable": false,
  "owner_action_required": true,
  "what_you_should_do": "**Do not retry.** Your user-agent has been banned by the site owner.",
  "footer": "This error was generated by Cloudflare on behalf of the website owner."
}
```

Raw response SHA-256: tools/list
`c3123731d602176e48a7b32636f37050ecda2382cb4a4b0c792a0554aead8c0d`;
plan `6af6559cafd06bfc78415059e1b2a14d82543d0e7be1921bf584550adede96cd`;
check `25dc85d4efa197eded5cc30e1801aacedf7cf771fde06bc4fb027523da73bf6f`.
No successful tools/list or tool envelope was received. Schema, live tool
parity, enforced limits and read-only execution remain **unverified**.
These failures warrant scope assessment, not an unrelated runtime repair.

A later public preflight owner can repeat these three single requests from
an allowed unauthenticated surface, changing only the date to the current
Europe/London date. Stop on a non-retryable denial. Compare returned tools,
input schemas and annotations to the source inventory above. Validate each
request against its advertised input schema, and successful structuredContent
against `PlanResponse` or `CheckResponse` in `/v1/openapi.json` (resolve local
references). Require matching JSON-RPC IDs, no error/isError, bounded days and
options/items, provenance and explicit unknowns where inputs are missing.
Retain responses and HTTP status. Inspect unexpected writes or upstream
behavior as discrepancies; tool hints alone do not prove read-only effects.

## Reproduce package checks

Run from the repository root on Linux. Outputs belong in `/tmp`; upload nothing.
Claude Code **2.1.284** `claude plugin validate --strict
projects/scotland-connector/plugin` passed on 2026-11-13 without warnings.
The portable manifest and MCP config passed Draft 2020-12 using
`jsonschema==4.26.0`; the candidate registry document above also passed its
published schema. These are offline validity results, separate from installation,
portal scans and acceptance. Codex **0.167.0** is present; no authenticated
client session was started.

| Retrieved contract | SHA-256 of successful fetch on 2026-11-13 |
| --- | --- |
| https://agent-plugins.org/schemas/1.0.0/plugin.schema.json | `0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883` |
| https://agent-plugins.org/schemas/1.0.0/mcp.schema.json | `6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb` |
| https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json | `3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0` |
| https://developers.openai.com/plugins/deploy/submission.md | `484e56805448ab8c1154998ded4914ec8c3155cda04e7f8bbc876104bd64c278` |
| https://developers.openai.com/plugins/build/plugins.md | `b913de64a45a90f14fba0d4b0654444f1f60f4e2d4f79f3dd41332895f79721d` |
| https://learn.chatgpt.com/docs/build-skills.md | `95b5ef4d0581d9eeeee342ef4718e77ed7574fe9c61ae28e1c2bffa4c6bdb411` |
| https://modelcontextprotocol.io/registry/authentication.md | `b05d60863a78f74892ed7341592a981d601fda4173c704fe6c43dd353293bcb6` |

The schema checks in READINESS.md reproduce portable validation. Save the
registry JSON block above as `/tmp/scotland-server.candidate.json`, fetch
its exact `$schema` and validate with the same `Draft202012Validator`:

```sh
curl -fLsS https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json -o /tmp/scotland-server.schema.json
/tmp/scotland-audit-venv/bin/python - <<'PY'
import json
from jsonschema import Draft202012Validator
schema = json.load(open('/tmp/scotland-server.schema.json'))
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(json.load(open('/tmp/scotland-server.candidate.json')))
print('Registry candidate schema: PASS; namespace authorization unverified')
PY
```

Create a ZIP from tracked package files, preserving dotfiles and paths with
no enclosing `plugin/` directory. Do not include this packet, credentials,
validator output or a generated `server.json` in the ZIP. This command was
run; the 11 entries matched current tracked bytes and ZIP CRC checks passed.
ZIP SHA-256 was `d6181ef726c39b9846ef18c43396ef2493ee3de933ed96b4fa002f0ebfc2185f`.

```sh
python3 - <<'PY'
import subprocess, zipfile
from pathlib import Path
root = Path('projects/scotland-connector/plugin')
paths = subprocess.check_output(['git', 'ls-files', '-z', '--', str(root)]).decode().split('\0')
with zipfile.ZipFile('/tmp/scotland-0.1.1.zip', 'w') as out:
    for name in sorted(filter(None, paths)):
        path = Path(name)
        entry = zipfile.ZipInfo(path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
        entry.external_attr = 0o100644 << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        out.writestr(entry, path.read_bytes())
with zipfile.ZipFile('/tmp/scotland-0.1.1.zip') as out:
    assert out.testzip() is None
    assert 'skills/scotland-trips/SKILL.md' in out.namelist()
    assert 'plugin.json' in out.namelist() and 'mcp.json' in out.namelist()
PY
sha256sum /tmp/scotland-0.1.1.zip
```

After exact approval, the publisher would upload that complete ZIP, connect
its no-auth MCP and inspect scans. No dashboard import was performed here.
Registry metadata can later be checked with a supported pinned
`mcp-publisher validate /tmp/scotland-server.candidate.json` after
confirming that installed version's help. Schema validation above is the
check actually performed; no publisher CLI was installed or logged in.

## Isolated client installation checks, pending

Prerequisites: approved existing client accounts/entitlements, authorized
credential use, supported Claude Code/Codex versions, network access to the
public service and a new empty project with no existing scotland skill or
server. Record client version, package revision, discovered skill path and
hash, tool inventory and transcripts. Do not copy account tokens into the
project or this ledger. Both real-client checks are **pending account approval**.

For Claude Code, after approval:

1. Create an isolated directory with `mktemp -d`, initialize a Git repository
   there and copy the existing `plugin/` folder into it. Record its revision.
2. Launch `claude --plugin-dir /absolute/isolated/plugin` from that project.
   Confirm `/skills` discovers `scotland:scotland-trips`, open the resolved
   `SKILL.md` and compare its SHA-256 with the tracked shared skill. Record
   the resolved name if the supported client displays it differently.
3. Confirm `/mcp` discovers scotland and its live tools; explicitly invoke
   the skill for one bounded plan/check case only after a successful public
   preflight. Record the selected tool and parameters.
4. Separately exercise the distributed import in that isolated project with
   `claude plugin marketplace add https://jomcgi.dev/scotland/marketplace.json`
   and `claude plugin install scotland@jomcgi --scope project`. Restart and
   verify the installed skill path/hash and server again. Local folder loading
   alone does not prove marketplace installation.

For Codex, the [current skill contract](https://learn.chatgpt.com/docs/build-skills)
loads project skills from `.agents/skills`, and `/skills` or `$` invokes them.
Prepare an isolated project without writing to the user's shared config:

```sh
scotland_install_dir=$(mktemp -d)
git init "$scotland_install_dir"
mkdir -p "$scotland_install_dir/.agents/skills" "$scotland_install_dir/client-config"
cp -R projects/scotland-connector/plugin/skills/scotland-trips "$scotland_install_dir/.agents/skills/"
cat > "$scotland_install_dir/client-config/config.toml" <<'TOML'
[mcp_servers.scotland]
url = "https://jomcgi.dev/scotland/mcp"
TOML
# Only after approved client account access and credential use:
CODEX_HOME="$scotland_install_dir/client-config" codex -C "$scotland_install_dir"
```

Authenticate only through the separately approved existing account flow.
In the new session verify `/skills` lists `scotland-trips` from that exact
project path, inspect/hash the file, then invoke `$scotland-trips` explicitly.
Confirm `/mcp` lists the server and run one approved bounded case after public
preflight. Save evidence that the instructions were loaded, not merely that
tools were listed. This tests manual skill plus MCP installation.

For full OpenAI plugin import, the official package guide supports a local
`.agents/plugins/marketplace.json` in a separate project: place the copied
package at `./plugins/scotland`, create one `plugins[]` entry with name
`scotland`, `source: {"source":"local","path":"./plugins/scotland"}`,
`policy: {"installation":"AVAILABLE","authentication":"ON_INSTALL"}` and
category candidate `Travel`; set the catalog `name` and
`interface.displayName`. Use the supported ChatGPT desktop Plugins Directory
local-source picker after restart to install it. Verify shared skill discovery
and the MCP in a new session. Desktop access, category selection and that import
are pending; this document claims neither ZIP installation nor account sync.
An MCP-only config, a custom connector or an MCP Registry entry cannot establish
installation of `skills/scotland-trips/SKILL.md`.

## Submission ledger

| Target | Preparation | Submission | Receipt | Review/listing outcome |
| --- | --- | --- | --- | --- |
| Claude plugin bundle | Source/path/branch and fields drafted; offline validator passed; eligible account unknown | **NOT SUBMITTED** | None | No outcome; account approval, policy accuracy/attestations, client installation and duplicate check pending |
| Claude remote connector | URL and form fields drafted; live discovery blocked | **NOT SUBMITTED** | None | No outcome; same account hold, all-tool client tests and compliance acknowledgments pending |
| OpenAI plugin | ZIP and schemas checked; drafts/test cases prepared; video absent | **NOT SUBMITTED** | None | No outcome; exact organization/project/identity, policies, installation, dashboard category/scans and duplicate check pending |
| MCP Registry | Remote-only metadata schema passed; namespace candidates unapproved; public search empty | **NOT SUBMITTED** | None | No outcome; namespace/credential authorization and authenticated duplicate check pending |

After authorization, append the approved non-secret decision reference, exact
submitted revision/package checksum, timestamp, submission ID, receipt URL,
review feedback and eventual public listing URL or rejection for each target.
Keep private screenshots/credentials in the private receipt. Do not infer an
outcome from an empty public search or a successful upload.

Operational acceptance stays on #7688: account/identity/namespace approval;
policy accuracy, rights, retention and exact attestations; both client skill
installations; authenticated duplicate checks; submissions; receipts; eventual
review/listing outcomes. The repository PR must use `Refs #7688` and carry
this outstanding checklist without a closing keyword.
