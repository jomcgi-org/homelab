# Trips

Photo-based GPS trip logging with elevation enrichment.

## Overview

| Component      | Description                                                                                |
| -------------- | ------------------------------------------------------------------------------------------ |
| **backend**    | FastAPI server that replays trip data from NATS JetStream and serves REST + WebSocket APIs; includes image upload endpoints (`UploadFile`) for ingesting trip photos |
| **frontend**   | Timeline view with day-by-day maps and per-day elevation stats (ascent, descent, min/max), plus a `TripSummaryPage` with a full-trip overview and multi-day map — deployed as a Cloudflare Pages app (not part of the Helm chart) |
| **tools**      | CLI tools for trip data management — six sub-directories: `publish-trip-images` (image ingestion with EXIF extraction), `backfill-elevation` (replays NATS points and enriches with elevation data), `delete-trip-points` (publishes tombstone messages to delete points), `publish-gap-route` (parses KML files to fill route gaps), `detect-wildlife` (wildlife detection inference + GoPro camera control), `elevation` (elevation API client library) |
| **chart**      | Helm chart for Kubernetes deployment — includes nginx reverse-proxy and imgproxy templates |
| **deploy**     | ArgoCD Application, kustomization, cluster-specific values, and SigNoz HTTP health check alert for imgproxy (`img-httpcheck-alert.yaml`) |
