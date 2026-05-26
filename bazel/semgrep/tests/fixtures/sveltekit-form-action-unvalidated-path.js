// Test fixtures for the sveltekit-form-action-unvalidated-path semgrep rule.
//
// Annotations:
//   // ruleid: sveltekit-form-action-unvalidated-path  — the next match MUST be flagged
//   // ok: sveltekit-form-action-unvalidated-path      — the next match MUST NOT be flagged

// BAD: path from form data passed directly into fetch() — SSRF vector
// ruleid: sveltekit-form-action-unvalidated-path
async function badDirect(request) {
  const data = await request.formData();
  const path = data.get("path");
  const res = await fetch("https://internal-service.local" + path, {
    method: "GET",
  });
  return res;
}

// BAD: no validation before the fetch call
// ruleid: sveltekit-form-action-unvalidated-path
async function badNoValidation(request) {
  const data = await request.formData();
  const path = data.get("path");
  const url = "https://internal-service.local" + path;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
  });
  return res.json();
}

// GOOD: path validated with startsWith before fetch
// ok: sveltekit-form-action-unvalidated-path
async function goodStartsWith(request) {
  const data = await request.formData();
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
// ok: sveltekit-form-action-unvalidated-path
async function goodAllowList(request) {
  const data = await request.formData();
  const path = data.get("path");
  if (!isAllowedPath(path)) {
    return { status: 400, error: "path not allowed" };
  }
  const res = await fetch("https://internal-service.local" + path, {
    method: "GET",
  });
  return res;
}
