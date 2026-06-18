import adapter from "@sveltejs/adapter-node";
import { mdsvex } from "mdsvex";
import { cspDirectives } from "./src/lib/csp.js";

const config = {
  extensions: [".svelte", ".svx"],
  preprocess: [mdsvex()],
  kit: {
    // Output dir is env-parametrized so the public build (:build_public) can
    // emit to a distinct directory and avoid colliding with :build's output in
    // the same Bazel package. Defaults to "build" for the normal build.
    adapter: adapter({
      out: process.env.SVELTE_OUT_DIR || "build",
    }),
    // Content-Security-Policy (ADR 005 layer 8, Phase 4c). mode "auto" nonces
    // SvelteKit's own inline bootstrap scripts under SSR, so script-src can stay
    // strict (no 'unsafe-inline') and untrusted public-chat output cannot run
    // script. The host allow-list and the style-relaxation rationale live in
    // src/lib/csp.js. This is global config (one svelte.config.js drives both
    // the public and private builds), so the directives are audited against all
    // routes, not just the public chat.
    csp: {
      mode: "auto",
      directives: cspDirectives,
    },
  },
};

export default config;
