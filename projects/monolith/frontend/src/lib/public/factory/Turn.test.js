// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import Turn from "./Turn.svelte";

let mounted;

afterEach(() => {
  if (mounted) unmount(mounted);
  document.body.innerHTML = "";
  mounted = null;
});

const DIFF = [
  "diff --git a/x.html b/x.html",
  "--- a/x.html",
  "+++ b/x.html",
  "@@ -1 +1,2 @@",
  " <p>kept</p>",
  "+<img src=x onerror=window.pwned=true>",
].join("\n");

function mountTurn(props) {
  const target = document.createElement("div");
  document.body.append(target);
  mounted = mount(Turn, { target, props });
  return target;
}

describe("Turn", () => {
  it("renders a reply's markdown and never its HTML", () => {
    const target = mountTurn({
      turn: {
        seq: 1,
        prompt: "Do <b>this</b>",
        result_text:
          "Done, see `a.py`.\n\n- [x] one <script>window.pwned = true</script>\n- [ ] two\n\n| k | v |\n|---|---|\n| 1 | 2 |",
        activities: [
          { type: "bash", command: "echo <s>hi</s>" },
          { type: "edit", file_path: "x.html" },
          { type: "tool_use", name: "mcp__agents__search_knowledge" },
        ],
        diff: DIFF,
        cost_usd: 0.5,
        usage: { input_tokens: 10, output_tokens: 20, cache_read_tokens: 0 },
      },
      full: true,
    });
    const text = target.textContent;
    expect(text).toContain("Done, see a.py.");
    expect(text).toContain("<script>window.pwned = true</script>");
    expect(text).toContain("Do <b>this</b>");
    expect(text).toContain("echo <s>hi</s>");
    expect(text).toContain("search_knowledge · agents");
    expect(text).toContain("<img src=x onerror=window.pwned=true>");
    expect(target.querySelector("script")).toBeNull();
    expect(target.querySelector("img")).toBeNull();
    expect(target.querySelector(".reply b, .ask b")).toBeNull();
    expect(target.querySelector(".act-rows s")).toBeNull();
    // Structure that markdown.js vouches for does render.
    expect(target.querySelector("code")?.textContent).toBe("a.py");
    expect(target.querySelectorAll("li.task")).toHaveLength(2);
    expect(target.querySelector("table")).not.toBeNull();
    // The diff arrives as rows with one file header and a tinted add line.
    expect(target.querySelectorAll(".dfile")).toHaveLength(1);
    expect(target.querySelector(".ln.add .tx")?.textContent).toBe(
      "<img src=x onerror=window.pwned=true>",
    );
    expect(window.pwned).toBeUndefined();
  });

  it("keeps only followable links out of a reply", () => {
    const target = mountTurn({
      turn: {
        seq: 2,
        result_text: "[bad](javascript:alert(1)) [ok](https://example.test/x)",
        activities: [],
      },
    });
    const links = [...target.querySelectorAll(".reply a")];
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("https://example.test/x");
    expect(target.textContent).toContain("bad");
  });

  it("folds a long prompt and points the digest at the session", () => {
    const target = mountTurn({
      turn: {
        seq: 3,
        prompt: "x".repeat(2000),
        result_text: "ok",
        activities: Array.from({ length: 9 }, (_, i) => ({
          type: "bash",
          command: `cmd ${i}`,
        })),
      },
      session: "/slop/factory/activity/1/node/1",
    });
    const ask = target.querySelector("details.ask");
    expect(ask).not.toBeNull();
    expect(ask.open).toBe(false);
    expect(target.querySelector(".ask .len")?.textContent).toBe("2.0k chars");
    expect(target.querySelectorAll(".act-rows > li")).toHaveLength(6);
    expect(target.querySelector("a.more")?.getAttribute("href")).toBe(
      "/slop/factory/activity/1/node/1",
    );
    expect(target.querySelector("a.more")?.textContent).toContain("3 more");
  });
});
