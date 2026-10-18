import { redirect } from "@sveltejs/kit";

// Hosts handled by reroute reach their tier's root directly. This fallback
// sends an otherwise unmapped root to the public route tree.
//
// 307 rather than 301: a permanent redirect would be cached by the browser and
// would outlive any future decision to serve something real at `/`.
export const load = () => {
  redirect(307, "/public/");
};
