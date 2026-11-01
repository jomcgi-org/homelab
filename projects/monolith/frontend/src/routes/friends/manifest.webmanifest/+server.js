import { manifestResponse } from "$lib/manifest.js";

export const prerender = false;

export function GET() {
  return manifestResponse("friends.jomcgi.dev", "friends");
}
