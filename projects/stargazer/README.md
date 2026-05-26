# Stargazer

Finds the best stargazing spots in Scotland for the next 72 hours.

## Overview

Multi-phase pipeline: light pollution atlas + OSM road data to identify dark zones near roads, scored by weather forecast. The backend runs as a scheduled CronJob (every 6 hours). An optional NGINX API server serves the results — disabled by default (`api.enabled: false` in chart defaults) and enabled in the cluster deploy values.

> **Note:** DEM (Digital Elevation Model / elevation) support is planned but not yet implemented. The pipeline currently uses `altitude_m = 0` for all locations. See [implementation.md](implementation.md) for planned tasks T4 and T8.

| Component   | Description |
| ----------- | ----------- |
| **backend** | Pipeline that combines light pollution data, OSM roads, and weather forecasts (`weather.py` handles met.no forecast API calls). Also contains `api.py`, a stdlib Python HTTP server (`/health`, `/api/locations`, `/api/best`) that was used during development; the deployed API uses NGINX instead (see `chart/templates/deployment-api.yaml`) |
| **tests**   | Additional unit tests for the backend pipeline (separate from tests co-located in `backend/`) |
| **chart**   | Helm chart with CronJob and optional NGINX API server templates (controlled by `api.enabled` flag) |
| **deploy**  | ArgoCD Application, kustomization, and cluster-specific values (enables API server via `api.enabled: true`) |

## Documentation

- [implementation.md](implementation.md) — Detailed design document with task specs and progress log
