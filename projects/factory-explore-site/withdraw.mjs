// Withdrawing the binding keeps the Worker available for a later release.
const hostname = "factory-explore.jomcgi.dev";
const service = "factory-explore";
const endpoint = new URL(
  `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/workers/domains`,
);
const headers = { Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}` };
const query = new URL(endpoint);
query.searchParams.set("hostname", hostname);
const response = await fetch(query, { headers });
if (!response.ok) throw new Error(`Domain lookup returned ${response.status}`);
const listing = await response.json();
if (!listing.success || !Array.isArray(listing.result))
  throw new Error("Domain lookup failed");
for (const domain of listing.result) {
  if (domain.hostname !== hostname || domain.service !== service) continue;
  if (!/^[a-f0-9]{32}$/i.test(domain.id)) throw new Error("Invalid domain ID");
  const removal = await fetch(`${endpoint}/${domain.id}`, {
    method: "DELETE",
    headers,
  });
  if (!removal.ok && removal.status !== 404)
    throw new Error(`Domain withdrawal returned ${removal.status}`);
}
