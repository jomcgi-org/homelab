const IDENTITY_HEADERS = [
  "cookie",
  "authorization",
  "cf-access-jwt-assertion",
  "x-auth-email",
];

export function requiresNoStore(event, response) {
  const id = event.route?.id;
  const hostname = new URL(event.request.url).hostname;
  const policies = [
    "cache-control",
    "cdn-cache-control",
    "cloudflare-cdn-cache-control",
  ];
  const restrictedPolicy = policies.some((header) =>
    /(?:^|,)\s*(?:private|no-store|no-cache)(?:\s*(?:=|,|$))/i.test(
      response.headers.get(header) || "",
    ),
  );
  const variesByIdentity = (response.headers.get("vary") || "")
    .toLowerCase()
    .split(",")
    .some(
      (name) => name.trim() === "*" || IDENTITY_HEADERS.includes(name.trim()),
    );

  return (
    !["GET", "HEAD"].includes(event.request.method) ||
    response.status >= 400 ||
    id === null ||
    /^\/(?:private|friends)(?:\/|$)/.test(id || "") ||
    hostname.startsWith("private.") ||
    hostname.startsWith("friends.") ||
    /^\/public\/app\/grimoire\/chat(?:\/|$)/.test(id || "") ||
    IDENTITY_HEADERS.some((name) => event.request.headers.has(name)) ||
    response.headers.has("set-cookie") ||
    restrictedPolicy ||
    variesByIdentity ||
    // App routes opt in with a policy at their loader/proxy. This includes
    // cookie-dependent HTML shells whose navigation data Kit already protects.
    (/^\/public\/(?:app|scotland)(?:\/|$)/.test(id || "") &&
      !response.headers.has("cache-control"))
  );
}
