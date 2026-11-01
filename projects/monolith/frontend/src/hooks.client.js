// A chunk from an earlier deploy failed to load: app.html reloads the page once
// past Cloudflare's cache. Any other error keeps SvelteKit's default handling.
export function handleError({ error, message }) {
  if (window.__freshReload?.(error))
    return { message: "Loading the latest version" };
  console.error(error);
  return { message };
}
