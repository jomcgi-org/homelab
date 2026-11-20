## Criterion 1

The factory chart test `test_discord_bridge_reaches_only_discord_its_memory_and_its_agent` renders the enabled path rule. The staging test in the same file checks the controller's seven-day idle session TTL and the policy before activation.

## Criterion 2

The Cilium v1.20.4 chart at upstream commit `beb276d2f38d4aadfda14710acf8d14a0f1ba723` rendered these data fields in the `cilium-config` ConfigMap with the factory values.

```yaml
hubble-redact-enabled: "true"
hubble-redact-http-headers-deny: "Authorization Proxy-Authorization Cookie Set-Cookie"
```

Reproduce from the repository root with a checkout of that commit.

```sh
helm template cilium "$CILIUM_CHECKOUT/install/kubernetes/cilium" -n kube-system \
  -f projects/platform-factory/cilium/values.yaml --show-only templates/cilium-configmap.yaml
```

After the Cilium rollout, verify header redaction on every node eligible for the bridge. Then enable `discord.bridge.enforceAgentPath` in the factory deployment values. With the updated bridge running, verify a Bosun turn completes and a POST from the bridge to another agent receives a Cilium HTTP 403. Inspect the matching Hubble flows for a redacted Authorization value.
