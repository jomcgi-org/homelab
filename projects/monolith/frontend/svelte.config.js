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
    }),
  },
};

export default config;
