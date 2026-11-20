# Capacity forecast architecture

```text
Prometheus history → Timestral worker → Prometheus scrape → Grafana
```

The worker requests up to seven days of reviewer memory at five-minute
intervals. It uses the largest pod working set at each observation. Each
batch publishes six five-minute predictions and a rolling p90 baseline.
The target timestamp identifies the observation that can score each point.
Missing observations stay missing.

The CPU-only image contains pinned model weights and their license. The
worker reads Prometheus and serves metrics over a cluster-local Service.
It has no Kubernetes credentials and changes no workload resources.

CI publishes the image and chart before Argo CD can deploy the chart.
After rollout, verify that Prometheus scrapes the worker and receives all
six Timestral horizons. Check the 30-minute comparison after its first
target observation arrives.
