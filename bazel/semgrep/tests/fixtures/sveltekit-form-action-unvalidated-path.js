// Test fixtures for the sveltekit-form-action-unvalidated-path semgrep rule.
//
// Annotations:
//   // ruleid: sveltekit-form-action-unvalidated-path  — the next non-annotation line MUST be flagged
//   // ok: sveltekit-form-action-unvalidated-path      — the next non-annotation line MUST NOT be flagged

// BAD: path from form data passed directly into fetch() — SSRF vector
async function badDirect(request) {
  const data = await request.formData();
  // ruleid: sveltekit-form-action-unvalidated-path
  const path = data.get("path");
  const res = await fetch("https://internal-service.local" + path, {
    method: "GET",
  });
  return res;
}

// GOOD: path validated with startsWith before fetch
async function goodStartsWith(request) {
  const data = await request.formData();
  // ok: sveltekit-form-action-unvalidated-path
  const path = data.get("path");
  if (!path.startsWith("/public/")) {
    return { status: 400, error: "invalid path" };
  }
  const res = await fetch("https://internal-service.local" + path, {
    method: "GET",
  });
  return res;
}

// GOOD: path validated with allow-list function before fetch
async function goodAllowList(request) {
  const data = await request.formData();
  // ok: sveltekit-form-action-unvalidated-path
  const path = data.get("path");
  if (!isAllowedPath(path)) {
    return { status: 400, error: "path not allowed" };
  }
  const res = await fetch("https://internal-service.local" + path, {
    method: "GET",
  });
  return res;
}
