// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import RevealEditor from "./RevealEditor.svelte";
import fixture from "./fixtures/reveal-projections.json";
import {
  buttonByText,
  chooseOption,
  plain,
  renderedFields,
  setChecked,
  settle,
  typeInto,
} from "./test-helpers.js";

const endpoint = "/grimoire/campaigns/camp/session/state";
const pc = { id: fixture.viewer, character_name: "Aria" };
const privateEntity = fixture.entities.private;
const dmEntity = {
  ...privateEntity,
  ...fixture.details.private,
  grants: [],
};

const json = (value) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

// A stand-in for the BFF: search, entity read, preview and reveal.
function stubFetch(projectionFor) {
  const fetch = vi.fn(async (url, options = {}) => {
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      if (body.operation === "previewReveal")
        return json([
          {
            player_character_id: pc.id,
            projection: projectionFor(body),
          },
        ]);
      return json([]);
    }
    if (String(url).includes("entity=")) return json(structuredClone(dmEntity));
    return json({
      items: [
        {
          id: privateEntity.id,
          name: privateEntity.name,
          entity_type: privateEntity.entity_type,
        },
      ],
    });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const posts = (fetch) =>
  fetch.mock.calls
    .filter(([, options]) => options?.method === "POST")
    .map(([, options]) => JSON.parse(options.body));

let instance;
async function openEntity() {
  instance = mount(RevealEditor, {
    target: document.body,
    props: { endpoint, characters: [pc], changed: vi.fn() },
  });
  await settle();
  document
    .querySelector("form")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await settle();
  buttonByText(
    document.body,
    `${privateEntity.name} ${privateEntity.entity_type}`,
  ).click();
  await settle();
  await setChecked(document.querySelector(`input[value="${pc.id}"]`));
}
const scopeSelect = () =>
  [...document.querySelectorAll("label")]
    .find((label) => label.textContent.startsWith("Knowledge scope"))
    .querySelector("select");
const fieldBox = (key) =>
  document.querySelector(`fieldset input[type="checkbox"][value="${key}"]`);

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("reveal preview matches the server projection", () => {
  const scopes = {
    full: "full",
    partial: "partial",
    name_only: "name_only",
    global_no_grant: "full",
  };
  for (const item of fixture.cases) {
    it(`renders the ${item.name} projection exactly`, async () => {
      stubFetch(() => item.expected);
      await openEntity();
      await chooseOption(scopeSelect(), scopes[item.name]);
      if (item.name === "partial") {
        await setChecked(fieldBox("disposition"));
        await typeInto(
          document.querySelector("textarea"),
          item.expected.revealed_details.clue,
        );
      }
      buttonByText(document.body, "Preview knowledge").click();
      await settle();
      const preview = document.querySelector(
        '[aria-label="Recipient previews"]',
      );
      expect(preview).not.toBeNull();
      expect(renderedFields(preview).sort()).toEqual(
        [...item.visible_fields].sort(),
      );
      const shown = item.expected.revealed_details || item.expected;
      for (const key of item.visible_fields) {
        const section = preview.querySelector(`[data-field="${key}"]`);
        expect(section.textContent).toContain(plain(shown[key]));
      }
      expect(preview.textContent).toContain(item.expected.name);
      expect(preview.textContent).toContain(item.expected.entity_type);
      for (const hidden of item.hidden_values || [])
        expect(preview.textContent).not.toContain(hidden);
      if (item.name === "name_only")
        expect(preview.textContent).toContain("You recognize this name.");
    });
  }
});

describe("partial field picker", () => {
  it("sends only the selected keys", async () => {
    const fetch = stubFetch(() => fixture.cases[1].expected);
    await openEntity();
    await chooseOption(scopeSelect(), "partial");
    const keys = [...document.querySelectorAll("fieldset input[type=checkbox]")]
      .map((box) => box.value)
      .filter((value) => value !== pc.id);
    expect(keys.length).toBeGreaterThanOrEqual(3);

    await setChecked(fieldBox("occupation"));
    buttonByText(document.body, "Preview knowledge").click();
    await settle();
    buttonByText(document.body, "Share knowledge").click();
    await settle();

    const reveal = posts(fetch).find((body) => body.operation === "reveal");
    expect(reveal.scope).toBe("partial");
    expect(reveal.revealedDetails).toEqual({
      occupation: fixture.details.private.occupation,
    });
  });

  it("adds the typed clue and nothing else", async () => {
    const fetch = stubFetch(() => fixture.cases[1].expected);
    await openEntity();
    await chooseOption(scopeSelect(), "partial");
    await setChecked(fieldBox("race"));
    await typeInto(document.querySelector("textarea"), " Seen at night ");
    buttonByText(document.body, "Preview knowledge").click();
    await settle();
    buttonByText(document.body, "Share knowledge").click();
    await settle();

    const reveal = posts(fetch).find((body) => body.operation === "reveal");
    expect(reveal.revealedDetails).toEqual({
      race: fixture.details.private.race,
      clue: "Seen at night",
    });
  });
});
