1. `projects/scotland-connector/deploy/application.yaml` pins chart 0.59.1
   and loads `projects/scotland-connector/factory/values.yaml` last. That file
   disables `createNamespace`, which guards `chart/templates/namespace.yaml`
   under the same project. The Application requests restricted Pod Security
   labels through `managedNamespaceMetadata` and enables `CreateNamespace=true`.
   Live confirmation is pending. Before merge, collect the factory Application
   and Namespace from the scoped factory session:

   ```sh
   export KUBECONFIG="$HOME/.kube/factory-sessions.yaml"
   kubectl -n argocd get applications.argoproj.io scotland-connector -o yaml
   kubectl get namespace scotland-connector --show-managed-fields -o yaml
   ```

   Record the successful sync revision and value files, the Namespace UID and
   Argo CD's ownership of the configured labels. Check the Application's
   resource status for a Namespace marked `requiresPruning` and confirm the
   synced chart's desired manifests omit the Namespace.

2. `projects/platform-factory/kustomization.yaml` includes the factory
   Application. Its `spec.syncPolicy.automated.prune` is true in
   `projects/scotland-connector/deploy/application.yaml`.
