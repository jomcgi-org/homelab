1. `//projects/shared/helm/factory-library:library_test` checks the default
   ResourceQuota and LimitRange against goldens and renders partial overrides.
2. The chart tests under `projects/{factory-explore,factory-explore-site,factory-landing,factory-public,factory-storyboard,hikes,jomcgi-site,ships,stars-forecast,trips}/chart`
   check both rendered kinds and their release namespace. The Storyboard
   deployment disables the pair because its destination is the factory platform
   namespace (`projects/factory-storyboard/deploy/application.yaml`).
3. `projects/platform-factory/kustomization.yaml` registers the application
   owners. The Scotland connector Application syncs its pair from
   `projects/scotland-connector/factory/resources`. The Grimoire data
   Application syncs its pair from
   `projects/platform-factory/grimoire-data/resources/kustomization.yaml`.
   The factory root syncs the production site pair through
   `projects/jomcgi-site/deploy/kustomization.yaml` at wave 5, after the
   production Application at wave 4. Its chart values disable the helper so
   the root retains ownership across Worker promotions.
   The other application namespaces receive the pair from their Helm releases.
   After the root and the affected chart releases sync,
   run the following for factory-explore, factory-explore-site, factory-landing,
   factory-public, grimoire, hikes, jomcgi-site-dev, jomcgi-site-prod,
   scotland-connector, ships, stars-forecast and trips. Check controller events since
   sync for refused pod creation and check workload readiness.

   ```sh
   kubectl -n "$namespace" get resourcequota,limitrange -o yaml
   kubectl -n "$namespace" get events --field-selector type=Warning --sort-by=.metadata.creationTimestamp
   kubectl -n "$namespace" get pods
   ```
