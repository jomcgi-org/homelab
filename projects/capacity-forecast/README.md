# Capacity forecast

Capacity forecast runs a read-only Timestral 5M worker for the factory
reviewer pool. It publishes p90 memory forecasts and a rolling p90 baseline
for comparison in Grafana.

See [Architecture](ARCHITECTURE.md) for the data flow and rollout checks.
