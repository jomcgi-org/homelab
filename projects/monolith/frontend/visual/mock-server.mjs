import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

// Map a request pathname to a committed fixture file (pure; unit-tested).
const ROUTES = [
  ["/api/home/observability/stats", "fixtures/api/home_stats.json"],
  ["/api/dr-jobs/listings", "fixtures/api/dr_jobs_listings.json"],
  ["/api/wc2026/summary", "fixtures/api/wc2026_summary.json"],
  ["/api/hikes/walks", "fixtures/api/hikes_walks.json"],
  ["/api/ships/snapshot", "fixtures/api/ships_snapshot.json"],
  ["/api/stars/sites", "fixtures/api/stars_sites.json"],
  ["/api/trips/trips", "fixtures/api/trips_trips.json"],
  ["/api/knowledge/public/graph", "fixtures/api/knowledge_graph.json"],
  ["/api/grimoire/books/mm/read", "fixtures/api/grimoire_read.json"],
];
const PREFIX = [["/api/trips/trip/", "fixtures/api/trips_trip.json"]];

export function resolveFixture(pathname) {
  for (const [p, f] of ROUTES) if (pathname === p) return f;
  for (const [p, f] of PREFIX) if (pathname.startsWith(p)) return f;
  return null;
}

export function startMock(port) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    const fixture = resolveFixture(url.pathname);
    if (!fixture) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "no fixture", path: url.pathname }));
      return;
    }
    const body = readFileSync(join(HERE, fixture));
    res.writeHead(200, {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      etag: '"mock"',
      "last-modified": "Thu, 01 Jan 1970 00:00:00 GMT",
    });
    res.end(body);
  });
  return new Promise((resolve) =>
    server.listen(port, "127.0.0.1", () => resolve(server)),
  );
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.MOCK_PORT || 8099);
  startMock(port).then(() => console.log(`mock api on :${port}`));
}
