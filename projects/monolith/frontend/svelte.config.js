import adapter from "@sveltejs/adapter-node";
import { mdsvex } from "mdsvex";

const config = {
  extensions: [".svelte", ".svx"],
  preprocess: [mdsvex()],
  kit: {
    // Output dir is env-parametrized so the public build (:build_public) can
    // emit to a distinct directory and avoid colliding with :build's output in
    // the same Bazel package. Defaults to "build" for the normal build.
    adapter: adapter({
      out: process.env.SVELTE_OUT_DIR || "build",
      // Build-time gzip + brotli of static assets (/_app/*.js, CSS). These are
      // served by adapter-node's sirv layer BEFORE the SvelteKit handler, so the
      // hooks.server.js compression hook never sees them; precompress is how the
      // JS bundle stops shipping uncompressed. sirv serves the .br/.gz variant
      // when the client accepts it, at zero runtime cost.
      precompress: true,
    }),
    // No Content-Security-Policy is set by the app. The markdown renderer
    // (components/notes/markdown.js) HTML-escapes untrusted public-chat output
    // and note bodies and emits no raw HTML, which is the real XSS protection
    // (covered by markdown.test.js). A CSP hardening layer is deferred to a
    // later pass.
  },
};

export default config;
