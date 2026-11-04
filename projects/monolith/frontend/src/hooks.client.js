// A chunk from an earlier deploy failed to load: app.html reloads the page once
// past Cloudflare's cache, at the page being opened. Any other error keeps
// SvelteKit's default handling.
export function handleError({ error, event, message }) {
  if (window.__freshReload?.(error, event?.url?.href))
    return { message: "Loading the latest version" };
  console.error(error);
  return { message };
}
