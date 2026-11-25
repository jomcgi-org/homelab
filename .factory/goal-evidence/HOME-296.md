## Criterion 1

The factory chart test `test_discord_bridge_reaches_only_its_expected_peers` renders the enabled rules: POST to the Bosun A2A path and POST to the gRPC `SessionService/DeleteSession` method, which the bridge calls after each turn (HOME-162 "Short sessions"). The staging test in the same file checks the policy before activation.

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

After the Cilium rollout, verify header redaction on every node eligible for the bridge and verify the updated bridge image and chart are deployed. Set `discord.bridge.enforceAgentPath=true` in both `projects/factory/deploy/values.yaml` and `projects/factory/chart/values-homelab.yaml`. Refresh `projects/factory/deploy/render.sha256` using the command in `test_deploy_values_render_is_locked`.

After sync, confirm a Discord mention completes a Bosun turn. From the bridge pod's network identity, POST to `/agents/factory-r-homelab/coder` on the same controller. Record the HTTP 403 and the corresponding Hubble L7 denial with the bridge as source. For the successful Bosun POST, check the Authorization field is redacted before retaining any flow evidence.
