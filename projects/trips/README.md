# Trips

Photo-based GPS trip logging with elevation enrichment.

## Overview

| Component      | Description                                                                                |
| -------------- | ------------------------------------------------------------------------------------------ |
| **backend**    | FastAPI server that replays trip data from NATS JetStream and serves REST + WebSocket APIs |
| **frontend**   | Timeline view with day-by-day maps and per-day elevation stats (ascent, descent, min/max) — deployed as a Cloudflare Pages app (not part of the Helm chart) |
| **tools**      | CLI tools for trip data management — six sub-directories: `publish-trip-images` (image ingestion with EXIF extraction), `backfill-elevation` (replays NATS points and enriches with elevation data), `delete-trip-points` (publishes tombstone messages to delete points), `publish-gap-route` (parses KML files to fill route gaps), `detect-wildlife` (wildlife detection inference + GoPro camera control), `elevation` (elevation API client library) |
| **chart**      | Helm chart for Kubernetes deployment — includes nginx reverse-proxy and imgproxy templates |
| **deploy**     | ArgoCD Application, kustomization, and cluster-specific values                             |
