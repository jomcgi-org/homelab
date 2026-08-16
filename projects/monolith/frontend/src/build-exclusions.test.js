import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const BUILD_SOURCE = readFileSync(new URL("../BUILD", import.meta.url), "utf8");
const FRIENDS_EXCLUDE = '"src/routes/friends/**"';

function srcPublicExcludeList(source) {
  const srcPublic = source.match(
    /js_library\(\s*name = "src_public",[\s\S]*?exclude = \[([\s\S]*?)\],/,
  );
  if (!srcPublic) {
    throw new Error("could not find the src_public exclude list");
  }
  return srcPublic[1];
}

function assertFriendsExcluded(source) {
  if (!srcPublicExcludeList(source).includes(FRIENDS_EXCLUDE)) {
    throw new Error("src_public must exclude src/routes/friends/**");
  }
}

describe("public frontend source exclusions", () => {
  it("keeps friends routes out of the public source set", () => {
    expect(() => assertFriendsExcluded(BUILD_SOURCE)).not.toThrow();
  });

  it("fails its guard when the friends exclusion is removed", () => {
    const withoutFriends = BUILD_SOURCE.replace(FRIENDS_EXCLUDE, "");
    expect(() => assertFriendsExcluded(withoutFriends)).toThrow(
      "src_public must exclude src/routes/friends/**",
    );
  });
});
