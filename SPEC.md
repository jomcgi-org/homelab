# Sidecar Broker Token Integration (PR 3 of #4250, ADR 048)

## Overview
Integrate broker token fetching into the egress sidecar. The sidecar learns to resolve injected credentials from the token broker instead of only from static Secrets. This enables subscription-based OAuth tokens (codex ChatGPT) where the broker holds the single mutable token grant and dispenses short-lived access tokens to callers.

## Key References
- ADR 048: docs/decisions/agents/048-codex-oauth-token-broker.md
- Broker API: projects/embervm/tokenbroker/cmd/tokenbroker/main.go (GET /grants/<name>/token)
- Broker response: {access_token, expires_at}
- Catalog template: projects/embervm/chart/templates/_noded-pod.tpl
- Current chart values: projects/embervm/deploy/values.yaml (egress.secrets)

## Tasks

### 1. CATALOG: Add brokerGrant alternative source
**File**: projects/embervm/chart/templates/_noded-pod.tpl (search "EGRESS_SECRETS")

Each egress.secrets entry today is:
```
{header, valuePrefix, env, egressTo, secretRef}
```

Add a second source option:
```
brokerGrant: <grant-name>  # mutually exclusive with secretRef
```

Entries with `brokerGrant` render into EGRESS_SECRETS with:
- The `brokerGrant` field populated
- NO `env` variable rendered (only secretRef entries produce env vars)

Keep existing `secretRef` entries working identically.

Add chart-side validation: fail (with a clear error) if both `brokerGrant` and `secretRef` are set, OR if neither is set.

### 2. SIDECAR: Fetch and cache broker tokens
**File**: projects/firecracker/substrate/egress-proxy/cmd/swap.go (new file)

The sidecar needs to resolve header values for `brokerGrant` entries by calling the broker.

Create swap.go with:
- `secretEntry` struct that holds both `secretRef` and `brokerGrant` sources
- `live()` method on secretEntry: returns true if the value is resolved (either from secret or broker)
- Broker HTTP client: GET {BROKER_URL}/grants/<grant>/token
  - Response: {"access_token": "...", "expires_at": "2026-08-02T...Z"}
  - 503 or unreachable: fail closed (entry is dead, deny the host)
- Caching logic:
  - Cache the access token until 60 seconds before expires_at
  - Single-flight per grant: a burst of guest requests makes one broker call
  - Use sync.Mutex + time.Timer or similar for concurrency safety
- `BROKER_URL` comes from env var `EGRESS_TOKEN_BROKER_URL` (rendered from chart)
- Injection semantics (same as secretRef):
  - Fire only when guest sent the header for that egressTo host
  - Delete every guest value
  - Set valuePrefix + real value

### 3. MAIN: Load broker URL and wire it
**File**: projects/firecracker/substrate/egress-proxy/cmd/main.go

- Read `EGRESS_TOKEN_BROKER_URL` from env
- Pass it to the proxy struct
- Call the broker client for `brokerGrant` entries during secret resolution
- Fail closed: if broker is unreachable or returns 503, the entry is dead

### 4. CHART: Render EGRESS_TOKEN_BROKER_URL
**File**: projects/embervm/chart/templates/_noded-pod.tpl

- Add logic: if ANY egress.secrets entry has `brokerGrant`, render EGRESS_TOKEN_BROKER_URL
- Default to: `embervm-embervm-tokenbroker.embervm.svc.cluster.local:8080`
- Use the chart's `tokenBroker.fullname` helper if it exists and is easy; otherwise hardcode with a comment
- Verify the broker's CiliumNetworkPolicy ingress admits noded/noded-brick pods on 8080 (run `helm template` to confirm, do not assume)

### 5. DEPLOY VALUES: Convert OpenAI entry
**File**: projects/embervm/deploy/values.yaml (egress.secrets)

Today:
```yaml
- header: "Authorization"
  valuePrefix: "Bearer "
  env: "EMBER_OPENAI_API_KEY"
  egressTo:
    - "api.openai.com"
  secretRef:
    name: "embervm-embervm-egress"
    key: "OPENAI_API_KEY"
```

Change to:
```yaml
- header: "Authorization"
  valuePrefix: "Bearer "
  egressTo:
    - "api.openai.com"
  brokerGrant: "codex-cluster"
```

Add a comment explaining that the subscription grant now supplies it and an absent grant fail-closes.

Leave the Anthropic entry unchanged (still using `secretRef`).

### 6. TESTS: Egress-proxy behavior
**File**: projects/firecracker/substrate/egress-proxy/cmd/swap_test.go (new file)

Go tests using httptest to fake the broker and proxy. Follow the existing idiom in the package (main_test.go). Each test:

1. **Broker-sourced header injected on the cleartext lane**: Guest sends plaintext HTTP, sidecar fetches token from broker, injects Authorization header, re-originates TLS to 443.

2. **Cache reused within the window and refreshed after it**: Broker called once within the cache window, called again after expiry - 60s margin.

3. **Single-flight under concurrency**: 10 concurrent guest requests for the same brokerGrant entry make exactly one broker call; others wait on that one.

4. **Broker 503/unreachable denies the host**: Broker returns 503 or is completely unreachable; the entry is dead (live() = false); guest request is denied with no tunnel, no injection.

5. **SecretRef entry still behaves exactly as before**: A regular secretRef entry (like Anthropic) continues to work, reading from Secret.

6. **Malformed entry rejected at load**: Both brokerGrant and secretRef set: config load fails. Neither set: config load fails. Test this during secret parsing.

## Verification

### Egress-proxy tests
```bash
bb remote --os=linux --arch=amd64 test //projects/firecracker/substrate/egress-proxy/... --config=ci
```

All tests must pass.

### Chart rendering
```bash
helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml
```

Verify:
- `EGRESS_SECRETS` contains the brokerGrant entry (codex-cluster) with no env var
- `EGRESS_TOKEN_BROKER_URL` is set to the broker Service name
- `EGRESS_SECRETS` still contains the Anthropic entry unchanged with its env var

### Chart bump
Commit chart version bump last, as its own commit.

## Commit
One commit per task (6 total), last commit bumps the chart:
```
feat(embervm): egress sidecar draws injected credentials from the token broker (ADR 048, #4250 PR 3)
```

## Acceptance
- All egress-proxy tests pass
- `helm template` renders the expected EGRESS_SECRETS and EGRESS_TOKEN_BROKER_URL
- brokerGrant and secretRef are mutually exclusive; malformed entries fail at load
- Chart version bumped
