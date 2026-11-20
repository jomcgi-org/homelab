# Agent notifications

The factory connector posts to `POST /notify` on the in-cluster Discord bridge.
Set `discord.bridge.policy.notificationChannels.homelab` to the approved channel ID.
Provision a dedicated random caller password in the 1Password item selected by
`connector.notify.itemPath`, field `app-password`. Enable `connector.notify.enabled`
once the bridge and credential are available. Both workloads read the mounted
field on each request. The bridge accepts only the factory-connector identity.

The request contains Channel (homelab), Level (info, warn or error), and Text
with Key, Title and optional Status, Count and canonical Link. Both ends validate
the fixed vocabulary. Discord receives a Components V2 post with mentions
disabled and a stable nonce for short-window retry deduplication. Confirm the
connector startup smoke notification in the homelab channel after rollout.

## HOME-385 rollout verification

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
