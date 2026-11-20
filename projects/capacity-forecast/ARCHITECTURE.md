# Capacity forecast architecture

```mermaid
flowchart LR
    history[Prometheus history] --> worker[Timestral worker]
    worker --> scrape[Prometheus scrape]
    scrape --> grafana[Grafana]
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
Activation also requires the Prometheus scrape job and both Prometheus
network policy allowances. Allow the worker to query Prometheus on 9090
and Prometheus to scrape the worker Service on 8080.
After rollout, verify that Prometheus scrapes the worker and receives all
six Timestral horizons. Check the 30-minute comparison after its first
target observation arrives.

## Trust boundaries

| Component | Untrusted input | Credentials held |
| --- | --- | --- |
| Forecast worker | Prometheus memory samples | None |

## Direction

| Direction | Tracks | State |
| --- | --- | --- |
| Package and verify the Timestral worker. | [HOME-670](https://factory-plan.jomcgi.dev/factory/issue/HOME-670) | In review |
| Compare reviewer forecasts with observed memory. | [HOME-669](https://factory-plan.jomcgi.dev/factory/issue/HOME-669) | Pending rollout |
