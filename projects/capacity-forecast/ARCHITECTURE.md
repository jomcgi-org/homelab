# Capacity forecast architecture

```mermaid
flowchart LR
    history[Prometheus history] --> worker[Timestral worker]
    worker --> scrape[Prometheus scrape]
    scrape --> grafana[Grafana]
```

The worker requests up to seven days of memory at five-minute intervals.
A chart profile names a namespace and workload, a memory target query, and
optional observed signals. A target can return one workload series or
one series per pod. Signals join on pod name or apply to the whole
workload when they have no pod label. Each query accepts up to 32 series.

The reviewer profile tracks the largest pod. The Codex profile forecasts
each pod using that pod's preceding CPU usage as an optional signal.
Each batch publishes six five-minute p90 predictions and a rolling p90
baseline. Missing observations stay missing. A missing or sparse optional
signal leaves the memory-only prediction available.

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
| Forecast Codex memory per pod with an optional observed signal. | [HOME-673](https://factory-plan.jomcgi.dev/factory/issue/HOME-673) | In review |
