# Factory notification sink (#7935)

The implementation is staged off. Merge, chart publication and rollout are
separate from operational acceptance. HOME-4, HOME-6 and HOME-10 can use
`connector/notify.Client.Notify(ctx, level, Text{...})` after activation.

The sink calls the private monolith's `monolith_agent_notify` wire tool
(`monolith-agent-notify` in the gateway catalogue) through Cloudflare at
`https://factory-notify.jomcgi.dev/mcp/`. It uses the dedicated Authentik
`factory-notify` provider, client credentials with username `factory-notify-sa`
and that account's app password. No operator or Ember agent credential belongs
in the factory's Secret.

## HOME-385 rollout verification

Joe owns provisioning the notify credential below. The factory delivery owner
owns sink activation through the reviewed deployment values and confirmation
in Discord. The platform factory Prometheus rule `FactoryItsaplanOutage` also sends sustained
phase failures to the homelab Discord channel through Alertmanager, using
the existing `factory-alerts-discord` credential.

The connector's notify sink ships disabled in the deploy values, so its
outage callback sends nothing until activation. The activation step below
turns it on after the `factory-notify` app password exists. Joe accepted this
staging ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-385#comment-5528)).

The HOME-385 local replay used 22 ms HTTP latency, twenty held issues with one
shared open blocker, one deferred board and two PR checks. Counts are
coordinator HTTP requests per simulated sweep.

| Revision | Deferred | Admissions | Delivery | Total | Requests/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before pacing | 2 | 60 | 4 | 66 | 42.52 |
| With pacing and shared blocker reads | 2 | 41 | 4 | 47 | 10.63 |

After rollout the delivery owner samples
`select request_count, last_request from apikey where name='agent:Factory coordinator'`
several times over one minute during normal sweeps and records the readings on
HOME-385. The acceptance threshold is below 300. The chart's PostSync bootstrap sets
the verified or re-minted coordinator key to 1,000 requests per 1,000 ms.
A rollback to an older chart keeps that ceiling: the old bootstrap does not
set key limits, and the unpaced connector has run on 1,000 since the
2026-11-18 22:05Z stopgap. The upstream 100 ceiling is what caused the
outage, so no hook lowers it.

## Credential task for Joe

Owner: @joe. This worker has no Authentik administration or 1Password write
tools. After the hub's MCP blueprint has reconciled:

- [ ] Issue an Authentik app password for `factory-notify-sa`, which belongs
  only to `factory-notify`. Keep it out of issue comments, captured terminals
  and Git. This is an app password, not the OAuth provider's client secret.
- [ ] Save it in 1Password item `k8s-homelab/factory-notify`, field
  `app-password`. The factory's 1Password Operator must be able to read it.
  Its `OnePasswordItem` creates Secret `factory/factory-notify`.
- [ ] Arrange renewal before expiry. Rotation of the mounted Secret takes
  effect on the next token mint without a connector restart.

## Activation by the delivery owner

1. Publish these Authentik, monolith and factory changes through normal CI
   and landing. Follow the factory's chart/value split until its bundled
   values cutover; do not enable unknown fields against an older chart.
2. Route `factory-notify.jomcgi.dev` through the hub's Cloudflare tunnel to
   the private Envoy Gateway, without caching or a browser Access challenge
   on `/mcp/`. The monolith validates the bearer. No direct origin route or
   anonymous notification handler is needed.
3. Enable `factoryNotify.enabled` in the monolith deployment values. The
   exact `/mcp/` route, Envoy JWT policy and dedicated verifier switch on together. Verify
   issuer, JWKS and audience against the reconciled provider. The existing
   `mcp.jomcgi.dev` Context Forge configuration stays unchanged.
   Confirm no bearer, expired bearer and an unrelated issuer all fail before
   MCP discovery. The Envoy policy verifies issuer/audience/group and the
   monolith independently verifies identity, tool and default-channel scope.
4. After Joe's item exists, enable `connector.notify.enabled` in factory
   values. Keep `deploy/values.yaml` and `chart/values-homelab.yaml` identical
   until the bundled-values cutover. Only the connector mounts the password
   and gains Cilium egress to the notify and token hostnames on TCP 443.
5. Verify both releases' live revisions and health. After acquiring its writer
   lock the connector sends `Factory notify sink is live`, at `info`, with
   caller key `factory-notify-live-v1`. Failures log `notify smoke pending`
   and retry on a later loop without stopping dispatch.
6. Observe that message in the homelab Discord channel. Repeat with the same
   key on the same UTC date (a connector restart exercises durable dedupe).
   Confirm there is still one message. Record the message link, deployed
   revisions and replay result on #7935. Until then acceptance is outstanding;
   MCP `ok` proves enqueueing, not Discord delivery.

Disable `connector.notify.enabled` to stop sending. Removing the monolith
route/verification config and revoking the app password closes the hub side.
Existing queued rows retain their normal outbox delivery state.

## Caller contract

Empty configuration disables the client; partial settings fail. Levels are
`info`, `warn` or `error`. `Text` accepts only these fields:

| Field | Allowed value |
| --- | --- |
| Key | 1–256 bytes; hashed, never displayed |
| Title | `Factory notify sink is live`, `Factory itsaplan outage`, `Scheduled agent digest`, `Scheduled agent alert` |
| Status | Empty, `succeeded`, `failed`, `blocked`, `running` |
| Count | 0–9999; zero omitted |
| Link | Optional canonical homelab GitHub issue/PR or factory-plan issue URL, numeric/UUID ID, no query, fragment, userinfo or arbitrary host/path |

Use fixed titles, not raw issue titles that might contain private data.
Unknown JSON fields are discarded; invalid allowed fields reject the call.
There is no body, logs or error-text field. Rendered text is at most 1000 bytes.
The total deadline is 15 seconds, with four-second HTTP timeouts, three attempts
and 250/500 ms backoff. Redirects are refused. Errors include no response bodies,
caller text, URLs or credentials.

The enabled client requires `BeforeRequest`, wired to
`dispatch.Engine.RecordNotifyAttempt`. Before every HTTP request it checks the
writer epoch and commits a `notify_attempt` counter with only the hashed daily
key, fixed request phase, epoch and timestamp. No payload or credential is
stored. Writer loss or an audit failure prevents the request.

Dedupe uses `(UTC date, caller key)`, independent of level or message changes.
Retries reuse one key even across midnight. The bounded local cache is an
optimization: the monolith hashes the channel/key and atomically inserts into
the existing unique outbox key, so a fresh process or lost response cannot
enqueue a second row. A new UTC date permits a new occurrence. These messages
also use the outbox's `sending`/`uncertain` boundary: an ambiguous Discord send
is quarantined rather than automatically posted twice. This is at-most-once
after dispatch begins, not a guarantee of delivery.

## FactoryConnectorStalled

Check connector logs for admission, send and tick errors. Compare
`factory_connector_dispatched_runs` by agent with the configured concurrency.
If all slots are occupied, inspect those runs' task progress and lease
deadlines. If slots are free, inspect the oldest runnable run and its latest
send outcome. An accepted send or an empty runnable queue clears the alert.
