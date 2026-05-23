// Test fixtures for the sveltekit-public-route-fetches-private-knowledge-api semgrep rule.
//
// Annotations:
//   // ruleid: sveltekit-public-route-fetches-private-knowledge-api  — the next non-annotation line MUST be flagged
//   // ok: sveltekit-public-route-fetches-private-knowledge-api      — the next non-annotation line MUST NOT be flagged

// Positive examples — fetching private knowledge API endpoints (should be flagged)

// Bare graph endpoint:
// ruleid: sveltekit-public-route-fetches-private-knowledge-api
const res = await fetch('/api/knowledge/graph', { signal: AbortSignal.timeout(5000) });

// Notes endpoint without public prefix:
// ruleid: sveltekit-public-route-fetches-private-knowledge-api
const notes = await fetch('/api/knowledge/notes/recent', { signal: AbortSignal.timeout(5000) });

// Double-quoted graph endpoint:
// ruleid: sveltekit-public-route-fetches-private-knowledge-api
const data = await fetch("/api/knowledge/graph");

// Negative examples — safe calls using the visibility-filtered public endpoints

// ok: sveltekit-public-route-fetches-private-knowledge-api
const pubGraph = await fetch('/api/knowledge/public/graph', { signal: AbortSignal.timeout(5000) });

// ok: sveltekit-public-route-fetches-private-knowledge-api
const pubNotes = await fetch('/api/knowledge/public/notes/recent', { signal: AbortSignal.timeout(5000) });

// ok: sveltekit-public-route-fetches-private-knowledge-api
const pubData = await fetch("/api/knowledge/public/graph");
