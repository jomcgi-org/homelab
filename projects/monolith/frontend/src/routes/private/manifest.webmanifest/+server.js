import { manifestResponse } from "$lib/manifest.js";

export const prerender = false;

export function GET() {
  return manifestResponse("private.jomcgi.dev", "dashboard");
}
