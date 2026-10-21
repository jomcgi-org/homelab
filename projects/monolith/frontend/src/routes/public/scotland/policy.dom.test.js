// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import Landing from "./+page.svelte";
import Privacy from "./privacy/+page.svelte";
import Support from "./support/+page.svelte";
import Terms from "./terms/+page.svelte";
import {
  CONTACT_EMAIL,
  PRIVACY_URL,
  SUPPORT_URL,
  TERMS_URL,
  policyLinks,
  privacy,
  support,
  terms,
} from "./policy-data.js";

let instance;
let target;

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
});

async function render(Page) {
  instance = mount(Page, { target });
  await tick();
  return target;
}

const hrefs = (root) =>
  [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

const pages = [
  ["privacy", Privacy, privacy],
  ["terms", Terms, terms],
  ["support", Support, support],
];

describe("scotland policy pages", () => {
  it("publish at the URLs the directory listings use", () => {
    expect(PRIVACY_URL).toBe("https://jomcgi.dev/scotland/privacy");
    expect(TERMS_URL).toBe("https://jomcgi.dev/scotland/terms");
    expect(SUPPORT_URL).toBe("https://jomcgi.dev/scotland/support");
  });

  for (const [slug, Page, doc] of pages) {
    it(`${slug} is one sheet with its title and every section`, async () => {
      const root = await render(Page);
      expect(root.querySelector("main.td.sheet")).not.toBeNull();
      expect(root.querySelector("h1").textContent).toBe(doc.title);
      for (const s of doc.sections) {
        expect(root.querySelector(`#${s.id} h2`).textContent).toBe(s.heading);
      }
    });

    it(`${slug} indexes sections by anchors that exist`, async () => {
      const root = await render(Page);
      const anchors = [...root.querySelectorAll(".spine .index-list a")]
        .map((a) => a.getAttribute("href"))
        .filter((h) => h.startsWith("#"));
      expect(anchors).toEqual(doc.sections.map((s) => `#${s.id}`));
      for (const href of anchors) {
        expect(root.querySelector(href)).not.toBeNull();
      }
    });

    it(`${slug} links back to the connector and to the other two`, async () => {
      const root = await render(Page);
      const links = hrefs(root);
      expect(links).toContain("/scotland");
      for (const l of policyLinks) {
        if (l.href !== `/scotland/${slug}`) expect(links).toContain(l.href);
      }
    });
  }

  it("support gives a mail link to a person", async () => {
    const root = await render(Support);
    expect(hrefs(root)).toContain(`mailto:${CONTACT_EMAIL}`);
  });

  it("privacy says the access log keeps addresses and query strings", async () => {
    const root = await render(Privacy);
    const text = root.querySelector("#kept").textContent;
    expect(text).toContain("network address");
    expect(text).toContain("query string");
    expect(text).toContain("Request bodies");
  });

  it("the landing page links all three", async () => {
    const root = await render(Landing);
    const links = hrefs(root);
    for (const l of policyLinks) expect(links).toContain(l.href);
  });
});
