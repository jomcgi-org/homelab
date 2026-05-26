// Test fixtures for the sveltekit-server-hardcoded-api-base-fallback semgrep rule.
//
// Annotations:
//   // ruleid: sveltekit-server-hardcoded-api-base-fallback  — the next non-annotation line MUST be flagged
//   // ok: sveltekit-server-hardcoded-api-base-fallback      — the next non-annotation line MUST NOT be flagged

// Positive examples — env var with hardcoded URL fallback (should be flagged)

// Classic localhost fallback:
// ruleid: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// HTTPS external fallback is equally wrong:
// ruleid: sveltekit-server-hardcoded-api-base-fallback
const BASE_URL = process.env.BASE_URL || "https://api.example.com";

// Single-quoted localhost:
// ruleid: sveltekit-server-hardcoded-api-base-fallback
const svcUrl = process.env.SVC_URL || "http://localhost:3000/api";

// Bracket-notation env access with fallback:
// ruleid: sveltekit-server-hardcoded-api-base-fallback
const monolithUrl = process.env["MONOLITH_URL"] || "http://localhost:8080";

// Negative examples — correct patterns (should not be flagged)

// No fallback — will throw/return undefined if not set (correct):
// ok: sveltekit-server-hardcoded-api-base-fallback
const API_BASE_SAFE = process.env.API_BASE;

// Non-URL string fallback (not a service URL):
// ok: sveltekit-server-hardcoded-api-base-fallback
const ENV_NAME = process.env.ENV_NAME || "production";

// Empty string fallback:
// ok: sveltekit-server-hardcoded-api-base-fallback
const FEATURE_FLAG = process.env.FEATURE_FLAG || "";

// Nullish coalescing without URL:
// ok: sveltekit-server-hardcoded-api-base-fallback
const TIMEOUT = process.env.TIMEOUT ?? "5000";

// URL not from process.env:
// ok: sveltekit-server-hardcoded-api-base-fallback
const hardcoded = "http://localhost:8000";
