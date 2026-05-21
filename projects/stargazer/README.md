# Stargazer

Finds the best stargazing spots in Scotland for the next 72 hours.

## Overview

Multi-phase pipeline: light pollution atlas + OSM road data + DEM (Digital Elevation Model) data to identify dark zones near roads, scored by weather forecast. The backend runs as a scheduled CronJob (every 6 hours). An optional NGINX API server serves the results — disabled by default (`api.enabled: false` in chart defaults) and enabled in the cluster deploy values.

| Component   | Description |
| ----------- | ----------- |
| **backend** | Pipeline that combines light pollution data, OSM roads, DEM elevation data, and weather forecasts (`weather.py` handles met.no forecast API calls) |
| **tests**   | Additional unit tests for the backend pipeline (separate from tests co-located in `backend/`) |
| **chart**   | Helm chart with CronJob and optional NGINX API server templates (controlled by `api.enabled` flag) |
| **deploy**  | ArgoCD Application, kustomization, and cluster-specific values (enables API server via `api.enabled: true`) |

## Documentation

- [implementation.md](implementation.md) — Detailed design document with task specs and progress log
