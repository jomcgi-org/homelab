# Stargazer

Finds the best stargazing spots in Scotland for the next 72 hours.

## Overview

Multi-phase pipeline: light pollution atlas + OSM road data + DEM (Digital Elevation Model) data to identify dark zones near roads, scored by weather forecast. The backend runs as a scheduled CronJob with a separate NGINX API server for serving results.

| Component   | Description |
| ----------- | ----------- |
| **backend** | Pipeline that combines light pollution data, OSM roads, DEM elevation data, and weather forecasts |
| **tests**   | Additional unit tests for the backend pipeline (separate from tests co-located in `backend/`) |
| **chart**   | Helm chart with CronJob and NGINX API server templates |
| **deploy**  | ArgoCD Application, kustomization, and cluster-specific values |

## Documentation

- [implementation.md](implementation.md) — Detailed design document with task specs and progress log
