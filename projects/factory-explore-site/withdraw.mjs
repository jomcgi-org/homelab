// Withdrawing the binding keeps the Worker available for a later release.
const hostname = "factory-explore.jomcgi.dev";
const service = "factory-explore";
// Cloudflare does not document the ID format. Accept any URL-path-safe token
// (32-hex, a 36-character UUID and similar) and refuse anything else.
const safeId = /^[A-Za-z0-9_-]{8,64}$/;
const endpoint = new URL(
  `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/workers/domains`,
);
const headers = { Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}` };

// Describes an ID by length and character classes only, never its value.
function shape(id) {
  if (typeof id !== "string") return `type=${id === null ? "null" : typeof id}`;
  const classes = [
    [/[a-f]/, "hex-lower"],
    [/[g-z]/, "lower-non-hex"],
    [/[A-F]/, "hex-upper"],
    [/[G-Z]/, "upper-non-hex"],
    [/[0-9]/, "digit"],
    [/-/, "hyphen"],
    [/_/, "underscore"],
    [/[^A-Za-z0-9_-]/, "other"],
  ]
    .filter(([pattern]) => pattern.test(id))
    .map(([, name]) => name);
  return `length=${id.length} classes=${classes.join(",") || "none"}`;
}

let matched = false;
let current;
try {
  const query = new URL(endpoint);
  query.searchParams.set("hostname", hostname);
  const response = await fetch(query, { headers });
  if (!response.ok)
    throw new Error(`Domain lookup returned ${response.status}`);
  const listing = await response.json();
  if (!listing.success || !Array.isArray(listing.result))
    throw new Error("Domain lookup failed");
  for (const domain of listing.result) {
    if (domain.hostname !== hostname || domain.service !== service) continue;
    matched = true;
    current = domain.id;
    if (typeof domain.id !== "string" || !safeId.test(domain.id))
      throw new Error("Invalid domain ID");
    const removal = await fetch(
      `${endpoint}/${encodeURIComponent(domain.id)}`,
      { method: "DELETE", headers },
    );
    if (!removal.ok && removal.status !== 404)
      throw new Error(`Domain withdrawal returned ${removal.status}`);
  }
} catch (error) {
  if (matched) console.error(`withdraw: domain ID shape ${shape(current)}`);
  throw error;
}
