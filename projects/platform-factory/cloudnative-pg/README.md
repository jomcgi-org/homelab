# CloudNativePG render baseline

Run this command from the repository root with Helm 3.16.4 to reproduce the factory operator manifests.

```sh
projects/platform-factory/cloudnative-pg/render.sh > /tmp/cloudnative-pg-0.29.0.yaml
```

The script checks the 0.29.0 release archive, renders with the Application's release name, namespace and ordered values files, and checks the resulting digest before writing the manifests to standard output. The Kubernetes version follows `projects/factory-cluster/nodes/install-node.sh`.
