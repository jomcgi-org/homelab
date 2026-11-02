import { goto, preloadCode } from "$app/navigation";
import { page } from "$app/state";

// Only a failed navigation has a supported in-place retry. A lazy panel on the
// current route has no retry callback, and an obsolete build needs fresh HTML.
async function recoverNavigation(url) {
  if (url.origin !== location.origin || url.href === location.href)
    return false;
  let timer;
  const from = location.href;
  try {
    const loaded = await Promise.race([
      preloadCode(url.pathname).then(() => true),
      new Promise((resolve) => {
        timer = setTimeout(() => resolve(false), 1500);
      }),
    ]);
    if (!loaded) return false;
    if (location.href !== from && location.href !== url.href) return true;
    await goto(url, { keepFocus: true, noScroll: true });
    return page.status < 400 && page.url.href === url.href;
  } finally {
    clearTimeout(timer);
  }
}

// A chunk from an earlier deploy failed to load: app.html reloads the page once
// past Cloudflare's cache, at the page being opened. Any other error keeps
// SvelteKit's default handling.
export function handleError({ error, event, message }) {
  if (window.__freshReload?.(error, event?.url?.href, recoverNavigation))
    return { message: "Loading the latest version" };
  console.error(error);
  return { message };
}
