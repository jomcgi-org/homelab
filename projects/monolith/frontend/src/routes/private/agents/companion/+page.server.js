import { redirect } from "@sveltejs/kit";

// The companion is a mode of the console now. Redirect on the public path,
// not the internal /private prefix hooks.js reroutes onto, and keep the query.
export function load({ url }) {
  const params = new URLSearchParams(url.search);
  params.set("mode", "voice");
  redirect(307, `/agents?${params}`);
}
