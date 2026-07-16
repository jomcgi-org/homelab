# loom (deployment)

Deployment config for loom, a typed-object data platform (Rust, DataFusion,
Iceberg) whose source lives in a separate private repository. loom builds
with Buck2 and consumes this repo's [buck2/](../../buck2/) rules as an
external cell; this directory holds only the ArgoCD wiring that deploys its
images to the cluster.
