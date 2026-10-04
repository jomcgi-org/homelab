// Legacy JSON compatibility only. New callers use /slop/factory/data/activity.
// hooks.server.js prevents either representation at this HTML URL from being
// cached: varying on Accept alone does not separate Cloudflare cache entries.
export { GET } from "../data/activity/+server.js";
