// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import AccessPanel from "./AccessPanel.svelte";

let mounted;

afterEach(async () => {
  if (!mounted) return;
  await unmount(mounted.component);
  mounted.target.remove();
  mounted = undefined;
});

describe("moving access panel", () => {
  it("renders the short not recognised state for a 403", () => {
    const target = document.createElement("div");
    document.body.append(target);
    const component = mount(AccessPanel, {
      target,
      props: { status: "forbidden" },
    });
    mounted = { component, target };

    expect(target.querySelector("h1")?.textContent).toBe(
      "This account is not recognised",
    );
    expect(target.textContent).toContain(
      "Your signed-in account is not listed as a Crossing viewer.",
    );
  });
});
