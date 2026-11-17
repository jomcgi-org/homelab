# Import source

`2026-11-17.json` records the cache-settings rules supplied in
[HOME-192](https://factory-plan.jomcgi.dev/factory/issue/HOME-192), under
"Facts (read on 2026-11-17 with the `jomcgi-site-cloudflare` token)".
It copies the issue's single Monolith rule, in the reported order.
It is a historical baseline for Monolith validation, not a rollback snapshot.
`../cache-settings.json` remains the complete desired entrypoint.

The same issue reports cache-settings and rate-limit read/write access.
It reports no rate-limit entrypoint and expected 403 responses for transform,
redirect, origin and compression phases. The token is IP-filtered; cluster
pods use an allowed IPv4 address.

These are supplied observations, not a new export from this coding session.
The session has no Cloudflare credential or cluster inspection tools.
The issue does not separately report a zone-list permission check.
The apply reads the zone by name and account before accessing the entrypoint;
an unsuccessful lookup fails before any PUT.

Before writing, the apply compares every imported rule's settings and position
with this baseline. Drift, including a changed Monolith TTL or enabled flag,
stops the Job before PUT. A retained PVC stores the actual prior policy before
any write, including an existing data rule. Restoration uses that durable
snapshot and accepts only its original or last applied policy. The issue
import is never substituted for a missing snapshot.

Live acceptance remains with the HOME-192 delivery owner: confirm the dev sync
succeeds and the entrypoint matches `cache-settings.json`, including Monolith.
Local fixtures and chart tests do not establish token scope or live acceptance.
