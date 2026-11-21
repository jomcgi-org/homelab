# Bosun test Activity

Build the shell from the repository root with `pnpm install --frozen-lockfile` and
`VITE_DISCORD_CLIENT_ID=<application-id> pnpm --filter bosun-test-activity build`.
Serve this package's `dist/` over HTTPS. Enable Activities on the existing Bosun
application. Map the root prefix to the static host and the `/activity` prefix
to `bosun-activity-api.jomcgi.dev/activity`. Configure the OAuth redirect
for the embedded application in Discord's developer portal.

The gateway bridge needs a `client-secret` field on its discord-bot item.
Set `discord.bridge.activity.clientId` to the same application ID. Route the
API hostname through the factory Cloudflare tunnel to edge and enable
`discord.bridge.activity.enabled` for the test. Polls live in the bridge's
memory for 24 hours and expire on restart.

Joe invites two App Testers to a server with fewer than 25 members. Allowlist
its channel in the bridge policy. Tag Bosun with “make us a poll for Friday”,
open the same poll as both testers, and vote from each client. Both clients
should receive the counts and updated summary. Repeat a read and a vote with
a Discord token from a tester outside the launch guild. Both must return 403.
