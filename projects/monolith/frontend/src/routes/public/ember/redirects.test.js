import { describe, expect, it } from "vitest";
import { load as bazel } from "./bazel/+page.server.js";
import { load as postgres } from "./postgres/+page.server.js";

describe("retired Ember demo loaders", () => {
  it.each([
    ["bazel", bazel],
    ["postgres", postgres],
  ])(
    "keeps the permanent %s redirect including query handling",
    (name, load) => {
      for (const suffix of ["", "?query=select+1&run=true"]) {
        let redirect;
        try {
          load({ url: new URL(`https://jomcgi.dev/ember/${name}${suffix}`) });
        } catch (error) {
          redirect = error;
        }
        expect(redirect).toMatchObject({
          status: 308,
          location: "/ember/firecracker#replay",
        });
      }
    },
  );
});
