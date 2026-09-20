// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import Page from "./+page.svelte";

let mounted;

afterEach(() => {
  if (mounted) unmount(mounted);
  document.body.innerHTML = "";
  mounted = null;
});

describe("public work-item page", () => {
  it("renders untrusted text as text and omits private factory records", () => {
    const target = document.createElement("div");
    document.body.append(target);
    mounted = mount(Page, {
      target,
      props: {
        data: {
          document: {
            item: {
              id: 100123,
              title: "<script>window.pwned = true</script>",
              state: "ready",
              authority: "github",
              trust: "trusted",
              task_class: "bug-fix",
              labels: ["<svg onload=window.pwned=true>"],
              github_issue_number: 6257,
              source_ref: "https://github.com/owner/repo/issues/6257",
            },
            edges_in: [],
            edges_out: [],
            receipts: [{ escalation_json: "private escalation" }],
            events: [{ author: "operator@example.test" }],
          },
        },
      },
    });

    expect(target.textContent).toContain(
      "<script>window.pwned = true</script>",
    );
    expect(target.textContent).toContain("<svg onload=window.pwned=true>");
    expect(target.querySelector("script")).toBeNull();
    expect(target.querySelector("img")).toBeNull();
    expect(target.querySelector("svg[onload]")).toBeNull();
    expect(target.textContent).not.toContain("private escalation");
    expect(target.textContent).not.toContain("operator@example.test");
  });
});
