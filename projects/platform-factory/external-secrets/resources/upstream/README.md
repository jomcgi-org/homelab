# external-secrets 2.15.0

From the repository root, render for the factory Kubernetes version in `projects/factory-cluster/nodes/install-node.sh`.

```sh
wget -O /tmp/es.tgz https://github.com/external-secrets/external-secrets/releases/download/helm-chart-2.15.0/external-secrets-2.15.0.tgz
helm template external-secrets /tmp/es.tgz --namespace external-secrets --kube-version 1.37.2 --include-crds --skip-tests -f projects/platform-factory/external-secrets/values.yaml > projects/platform-factory/external-secrets/resources/upstream/external-secrets-2.15.0-install.yaml
```

Run `projects/platform-factory/external-secrets/verify-render.sh` to verify the release archive digest and compare the complete render.
