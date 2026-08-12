import { redirect } from "@sveltejs/kit";

// There is no page at `/`. The route tree starts at /public and /private, so a
// bare root has always 404'd, and nobody noticed because nothing served the
// root: jomcgi.dev is monolith-public and private.jomcgi.dev is entered at a
// deep link.
//
// dev.jomcgi.dev is the first host where the root IS the entry point, and
// landing on the 404 page there is a poor first impression of an environment
// whose whole job is to look like production.
//
// 307 rather than 301: a permanent redirect would be cached by the browser and
// would outlive any future decision to serve something real at `/`.
export const load = () => {
  redirect(307, "/public/");
};
