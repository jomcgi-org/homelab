# HOME-211 browser verification (PR #8183, review round 3)

On 2026-11-17, Chromium loaded the compiled build with production CSP on loopback.
Actual snapshot/publish functions supplied two v2 walks, one with retained old weather.
All four 390/1280 px × light/dark cases passed: no pre-intent data/map fetch,
automatic shared-link restoration, hourly card, per-walk stale warning,
pending/denied geolocation, lazy map import, usable fallback, and axe including contrast.
No uncaught errors, CSP violations or horizontal overflow; no-JS facts stayed visible.
Initial JS: 103.4 KiB gzip. MapLibre-mock tests cover markers and pan/zoom persistence.
Limits: Chromium lacked WebGL2, so real tiles/markers were not verified.
`next.jomcgi.dev` DNS returned `ENOTFOUND`. Criterion 15 remains for the delivery owner
after dev deployment: real map, both 301s with queries, location, freshness and themes.
