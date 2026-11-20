# cloudnative-pg 0.29.0

From the repository root, render for the factory Kubernetes version in `projects/factory-cluster/nodes/install-node.sh`.

```sh
wget -O /tmp/cn.tgz https://github.com/cloudnative-pg/charts/releases/download/cloudnative-pg-v0.29.0/cloudnative-pg-0.29.0.tgz
helm template cnpg /tmp/cn.tgz --namespace cnpg-system --kube-version 1.37.2 --include-crds --skip-tests -f projects/platform/cloudnative-pg/deploy/values.yaml -f projects/platform-factory/cloudnative-pg/values.yaml > projects/platform-factory/cloudnative-pg/resources/upstream/cloudnative-pg-0.29.0-install.yaml
```

Run `projects/platform-factory/cloudnative-pg/verify-render.sh` to verify the release archive digest and compare the complete render.
