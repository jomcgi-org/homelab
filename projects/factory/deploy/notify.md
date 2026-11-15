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
| Title | `Factory notify sink is live`, `Scheduled agent digest`, `Scheduled agent alert` |
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
