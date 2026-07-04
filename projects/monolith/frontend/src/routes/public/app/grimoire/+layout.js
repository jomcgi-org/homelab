// The public Grimoire is a Turnstile-gated, client-fetch data app: the corpus
// (books/sections/chunks) must never appear in server-rendered HTML before the
// visitor solves the challenge, so SSR is off for the whole route tree. Every
// child page fetches from /api/grimoire in the browser after admission.
export const ssr = false;
