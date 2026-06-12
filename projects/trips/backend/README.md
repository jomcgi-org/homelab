# Trips API

GPS trip tracking API with photo locations and real-time updates.

## Overview

Serves GPS waypoints from travel photos via REST API and WebSocket. Points are stored in NATS JetStream and replayed on startup to build an in-memory cache.

```mermaid
flowchart LR
    NATS[NATS JetStream] --> API[Trips API]
    API --> REST[REST Clients]
    API --> WS[WebSocket Clients]
```

## Key Features

- **Photo geolocation** - Extract GPS coordinates from image EXIF
- **EXIF metadata** - Capture camera settings (ISO, shutter, aperture)
- **Elevation data** - Enrich with NRCan CDEM elevation API
- **Real-time sync** - WebSocket broadcasts for live viewer updates
- **Stream replay** - Rebuilds state from NATS on startup
- **Viewer count** - Track connected WebSocket clients

## Stream Replay on Startup

On startup, the service replays all messages from the NATS JetStream to rebuild its in-memory cache of trip points.

```mermaid
sequenceDiagram
    participant API as Trips API
    participant NATS as NATS JetStream
    participant Cache as In-Memory Cache

    Note over API: Service starts
    API->>NATS: Subscribe to stream (from beginning)
    activate NATS
    loop Replay all messages
        NATS-->>API: Trip point message
        API->>Cache: Add point to cache
    end
    deactivate NATS
    Note over API: Replay complete, ready to serve
    API->>API: Start HTTP/WebSocket server
```

**Why stream replay?**

- No database needed - NATS JetStream is the source of truth
- Fast startup - rebuilds entire dataset in seconds
- Crash recovery - always starts with consistent state
- Simplified architecture - no dual-write to DB + NATS

**JetStream configuration:**

```yaml
# Stream retention: 1 year
Retention: LimitsPolicy
MaxAge: 8760h # 365 days
```

**Startup sequence:**

1. Connect to NATS
2. Subscribe to `trips.>` stream from sequence 0
3. Process all existing messages (add to cache)
4. Continue consuming new messages in real-time
5. Start HTTP server when replay complete

## WebSocket Protocol

### Connection

```javascript
const ws = new WebSocket("wss://api.jomcgi.dev/trips/ws/live");
```

**Authentication:** None (public read-only endpoint)

### Message Types

#### Server → Client: Connected

Sent immediately upon connection.

```json
{
  "type": "connected",
  "cached_points": 1523
}
```

#### Server → Client: New Point

Sent when a new trip point is added via the upstream pipeline.

```json
{
  "type": "new_point",
  "point": {
    "id": "abc123def456",
    "lat": 49.2827,
    "lng": -123.1207,
    "timestamp": "2024-01-15T12:00:00Z",
    "image": "photo.jpg",
    "source": "gopro",
    "tags": ["car"],
    "elevation": 125.5,
    "light_value": 8.6,
    "iso": 393,
    "shutter_speed": "1/240",
    "aperture": 2.5
  }
}
```

#### Server → Client: Delete Point

Sent when a trip point is removed (tombstone message received from NATS).

```json
{
  "type": "delete_point",
  "id": "abc123def456"
}
```

#### Server → Client: Viewer Count Update

Sent when the number of connected WebSocket clients changes.

```json
{
  "type": "viewer_count",
  "count": 3
}
```

#### Client → Server: Ping

Optional heartbeat to keep connection alive. Send the plain text string `"ping"` (not JSON).

```
ping
```

#### Server → Client: Pong

Response to client ping. Returns the plain text string `"pong"` (not JSON).

```
pong
```

### Example Client

```javascript
const ws = new WebSocket("wss://api.jomcgi.dev/trips/ws/live");

ws.onopen = () => {
  console.log("Connected to trip updates");
};

ws.onmessage = (event) => {
  // Pong is a plain text response, not JSON
  if (event.data === "pong") {
    console.log("Server alive");
    return;
  }

  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "connected":
      console.log("Ready, cached points:", msg.cached_points);
      break;

    case "new_point":
      console.log("New location:", msg.point.lat, msg.point.lng);
      // Update map marker
      addMarker(msg.point.lat, msg.point.lng);
      break;

    case "delete_point":
      console.log("Removed point:", msg.id);
      removeMarker(msg.id);
      break;

    case "viewer_count":
      console.log("Active viewers:", msg.count);
      // Update UI badge
      updateViewerCount(msg.count);
      break;
  }
};

ws.onerror = (error) => {
  console.error("WebSocket error:", error);
};

ws.onclose = () => {
  console.log("Connection closed, reconnecting...");
  // Implement exponential backoff reconnect
};

// Optional: Send heartbeat every 30s (plain text, not JSON)
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send("ping");
  }
}, 30000);
```

## API Endpoints

All REST endpoints require authentication via the `X-API-Key` header (when `TRIP_API_KEY` is configured). The WebSocket endpoint is unauthenticated.

### GET /api/points

Get all trip points.

**Headers:**

- `X-API-Key: <API_KEY>` (required when auth is configured)

**Response:**

```json
{
  "points": [
    {
      "id": "abc123def456",
      "lat": 49.2827,
      "lng": -123.1207,
      "timestamp": "2024-01-15T12:00:00Z",
      "image": "photo.jpg",
      "source": "gopro",
      "tags": ["car"],
      "elevation": 125.5,
      "light_value": 8.6,
      "iso": 393,
      "shutter_speed": "1/240",
      "aperture": 2.5
    }
  ],
  "total": 1
}
```

**Query parameters:**

- `limit` (optional) - Max points to return (default: all)
- `offset` (optional) - Number of points to skip (default: 0)

**Examples:**

```bash
# Get all points
curl -H "X-API-Key: $TRIP_API_KEY" https://api.jomcgi.dev/trips/api/points

# Get first 100 points
curl -H "X-API-Key: $TRIP_API_KEY" https://api.jomcgi.dev/trips/api/points?limit=100

# Paginate: skip first 100, get next 100
curl -H "X-API-Key: $TRIP_API_KEY" https://api.jomcgi.dev/trips/api/points?limit=100&offset=100
```

### GET /api/points/{point_id}

Get a single trip point by ID.

**Headers:**

- `X-API-Key: <API_KEY>` (required when auth is configured)

**Response:** A single `TripPoint` object, or `404` if not found.

### GET /api/stats

Get trip statistics.

**Headers:**

- `X-API-Key: <API_KEY>` (required when auth is configured)

**Response:**

```json
{
  "total_points": 1523,
  "connected_clients": 3
}
```

### GET /health

Health check endpoint (unauthenticated).

**Response:**

```json
{
  "status": "healthy",
  "points": 1523,
  "connected_clients": 3
}
```

### WS /ws/live

WebSocket endpoint for real-time updates (see WebSocket Protocol section above).

## Data Model

### Trip Point

| Field           | Type   | Description                 | Required | Example                    |
| --------------- | ------ | --------------------------- | -------- | -------------------------- |
| `id`            | string | Unique identifier (UUID v5) | Yes      | `abc123def456`             |
| `lat`           | float  | Latitude                    | Yes      | `49.2827`                  |
| `lng`           | float  | Longitude                   | Yes      | `-123.1207`                |
| `timestamp`     | string | ISO 8601 timestamp          | Yes      | `2024-01-15T12:00:00Z`     |
| `image`         | string | Image filename              | No       | `photo.jpg`                |
| `source`        | string | Image source                | Yes      | `gopro`, `camera`, `phone` |
| `tags`          | array  | Classification tags         | No       | `["car", "highway"]`       |
| `elevation`     | float  | Elevation (meters)          | No       | `125.5`                    |
| `light_value`   | float  | Exposure value              | No       | `8.6`                      |
| `iso`           | int    | ISO sensitivity             | No       | `393`                      |
| `shutter_speed` | string | Shutter speed               | No       | `1/240`                    |
| `aperture`      | float  | F-stop                      | No       | `2.5`                      |

**ID generation:**

```python
import uuid

# Deterministic ID based on source + timestamp + filename
id = uuid.uuid5(
    namespace=uuid.NAMESPACE_URL,
    name=f"{source}:{timestamp}:{filename}"
)
```

### EXIF Extraction

Extracted from image metadata:

```python
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

image = Image.open("photo.jpg")
exif = image._getexif()

# GPS coordinates
gps_info = exif[34853]  # GPSInfo tag
lat = gps_info[2]  # GPSLatitude
lng = gps_info[4]  # GPSLongitude

# Camera settings
iso = exif[34855]  # ISOSpeedRatings
shutter = exif[33434]  # ExposureTime
aperture = exif[33437]  # FNumber
```

### Elevation Enrichment

Queries NRCan CDEM API for elevation:

```python
import httpx

async def get_elevation(lat: float, lng: float) -> float:
    url = "https://geogratis.gc.ca/services/elevation/cdem/altitude"
    params = {"lat": lat, "lon": lng}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        data = response.json()
        return data["altitude"]
```

**Fallback:** If API fails, elevation is `null`.

## Configuration

Environment variables:

| Variable       | Description          | Default                                      | Required              |
| -------------- | -------------------- | -------------------------------------------- | --------------------- |
| `NATS_URL`     | NATS server URL      | `nats://localhost:4222`                      | Yes                   |
| `CORS_ORIGINS` | Allowed CORS origins | `http://localhost:5173,http://localhost:3000` | No                    |
| `TRIP_API_KEY` | API key for REST API | (none)                                       | No (disables auth if empty) |

## Running Locally

```bash
# Start dependencies
docker run -d --name nats -p 4222:4222 nats:latest -js

# Run service
export NATS_URL=nats://localhost:4222
export TRIP_API_KEY=test-key
bazel run //projects/trips/backend:main

# Test endpoints
curl http://localhost:8000/health
curl http://localhost:8000/api/points
```

## Deployment

Deployed via ArgoCD to Kubernetes cluster.

**Resources:**

- Helm chart: `projects/trips/chart/`
- Overlay: `projects/trips/deploy/`
- Service URL: https://api.jomcgi.dev/trips

**Dependencies:**

- NATS JetStream (deployed via ArgoCD)
- Cloudflare Access (authentication for production)

## Observability

### Traces

Instrumented with OpenTelemetry (auto-injected by Kyverno):

- HTTP request traces
- NATS message processing
- Elevation API calls
- WebSocket connections

View in SigNoz: https://private.jomcgi.dev/app/signoz

### Logs

Standard Python logging:

```
INFO:trips_api:Catchup complete. 1523 points loaded.
INFO:trips_api:WebSocket client connected. Total: 1
```

## Related Services

- **trips.jomcgi.dev** - Frontend map viewer (WebSocket client)
- **publish-trip-images** - Local script for bulk photo uploads
- **ships_api** - Similar pattern for AIS vessel tracking
