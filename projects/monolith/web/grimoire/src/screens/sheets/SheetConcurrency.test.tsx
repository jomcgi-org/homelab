import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { flushSync } from "react-dom";
import { describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { findToast, openApp } from "../../test/app";
import { campaigns, lobby } from "../../test/fixtures";
import {
  miraDraft,
  miraSheet,
  playerHistory,
  sheetsRoutes,
} from "../../test/fixtures/sheets";

const HISTORY =
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets";
const UPDATE =
  "PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}";
const CREATE =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts";
const SUBMIT =
  "POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/submit";
const nextTime = "2026-11-05T18:00:00.123456Z";
const remote = {
  ...miraDraft,
  sheet: {
    ...miraSheet,
    ability_scores: { ...miraSheet.ability_scores, intelligence: 18 },
  },
  updated_at: nextTime,
};

function open(routes: Routes = {}) {
  return openApp(
    {
      ...sheetsRoutes,
      "GET /lobby": lobby({ campaigns: [campaigns.joined] }),
      [HISTORY]: playerHistory([miraDraft]),
      ...routes,
    },
    "/sheets",
  );
}

const score = (name: string) =>
  screen.getByRole<HTMLInputElement>("spinbutton", { name });
const refreshed = (check: () => void) => waitFor(check, { timeout: 6_000 });

describe("Sheet changes in another tab", { timeout: 15_000 }, () => {
  it("commits native numeric input when capture updates flush before the bubble phase", async () => {
    const { backend, user } = open({ [UPDATE]: remote });
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    // The real browser flushes capture-phase React updates before its input
    // change handler runs. happy-dom batches those phases unless we expose
    // that boundary with a native target listener.
    const flushCapture = () => flushSync(() => {});
    for (const [field, value] of [
      [level, "4"],
      [score("INT"), "18"],
    ] as const) {
      await user.click(field);
      field.addEventListener("input", flushCapture, { capture: true });
      try {
        fireEvent.input(field, { target: { value } });
        await user.tab();
        expect((field as HTMLInputElement).value).toBe(value);
      } finally {
        field.removeEventListener("input", flushCapture, { capture: true });
      }
    }
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({
      level: 4,
      ability_scores: { intelligence: 18 },
    });
  });

  it("merges a remote ability with a local level and saves against that exact revision", async () => {
    const { backend, user } = open({ [UPDATE]: remote });
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    await user.tab();
    await user.click(level);
    backend.set({ [HISTORY]: playerHistory([remote]) });

    await refreshed(() => expect(score("INT").value).toBe("18"));
    expect(score("Level").value).toBe("4");
    expect(document.activeElement).toBe(level);
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({
      level: 4,
      ability_scores: { intelligence: 18 },
      expected_updated_at: nextTime,
    });
  });

  it("requires an explicit choice for overlapping changes and keeps unrelated local edits", async () => {
    const { backend, user } = open();
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    await user.clear(score("STR"));
    await user.type(score("STR"), "10");
    await user.tab();
    backend.set({
      [HISTORY]: playerHistory([
        { ...remote, sheet: { ...remote.sheet, level: 5 } },
      ]),
    });

    const conflict = await screen.findByRole(
      "region",
      { name: "Resolve sheet changes" },
      { timeout: 6_000 },
    );
    expect(
      screen
        .getByRole("button", { name: "Save draft" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      screen
        .getByRole("button", { name: "Submit to DM" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(
      within(conflict).getByText("Level: your edit 4; saved value 5."),
    ).toBeTruthy();
    await user.click(
      within(conflict).getByRole("button", { name: "Use saved values" }),
    );
    expect(score("Level").value).toBe("5");
    expect(score("STR").value).toBe("10");
    expect(score("INT").value).toBe("18");
    expect(
      screen.queryByRole("region", { name: "Resolve sheet changes" }),
    ).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "Save draft" })
        .hasAttribute("disabled"),
    ).toBe(false);
    expect(backend.callsTo(UPDATE)).toHaveLength(0);
  });

  it("does not silently recommit a focused number that matched the old saved value", async () => {
    const { backend, user } = open({ [UPDATE]: remote });
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(score("STR"));
    await user.type(score("STR"), "10");
    await user.clear(level);
    await user.type(level, "3");
    backend.set({
      [HISTORY]: playerHistory([
        { ...remote, sheet: { ...remote.sheet, level: 5 } },
      ]),
    });
    const conflict = await screen.findByRole(
      "region",
      { name: "Resolve sheet changes" },
      { timeout: 6_000 },
    );
    expect(document.activeElement).toBe(level);
    expect(
      screen
        .getByRole("button", { name: "Save draft" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await user.click(
      within(conflict).getByRole("button", { name: "Use saved values" }),
    );
    expect(score("Level").value).toBe("5");
    expect(score("STR").value).toBe("10");
    expect(score("INT").value).toBe("18");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({
      level: 5,
      ability_scores: { strength: 10, intelligence: 18 },
      expected_updated_at: nextTime,
    });
  });

  it("keeps a copyable dirty buffer after remote submission and restores it after return", async () => {
    const { backend, user } = open();
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    await user.tab();
    backend.set({
      [HISTORY]: playerHistory([{ ...remote, status: "submitted" }]),
    });

    const recovery = await screen.findByRole(
      "region",
      { name: "Unsaved sheet changes" },
      { timeout: 6_000 },
    );
    const copy = within(recovery).getByRole<HTMLTextAreaElement>("textbox", {
      name: "Your unsaved sheet changes",
    });
    expect(copy.readOnly).toBe(true);
    expect(copy.value).toContain("Level: 4");
    expect(copy.value).toContain("INT: 18");
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save draft" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Submit to DM" })).toBeNull();
    await user.click(
      within(recovery).getByRole("button", { name: "Copy my unsaved changes" }),
    );
    expect(await navigator.clipboard.readText()).toBe(copy.value);
    await findToast("Unsaved sheet changes copied.");
    expect(backend.callsTo(UPDATE)).toHaveLength(0);
    expect(backend.callsTo(CREATE)).toHaveLength(0);
    expect(backend.callsTo(SUBMIT)).toHaveLength(0);

    backend.set({
      [HISTORY]: playerHistory([{ ...remote, status: "returned" }]),
    });
    await refreshed(() => expect(score("Level").value).toBe("4"));
    expect(score("INT").value).toBe("18");
    expect(
      screen.getByRole("button", { name: "Start new draft" }),
    ).toBeTruthy();
    const newId = "67777777-7777-4777-8777-777777777777";
    const createdAt = "2026-11-05T19:00:00.123456Z";
    backend.set({
      [CREATE]: (call) => {
        const created = {
          ...remote,
          id: newId,
          version: miraDraft.version + 1,
          status: "draft" as const,
          sheet: call.body as Record<string, unknown>,
          updated_at: createdAt,
        };
        backend.set({ [HISTORY]: playerHistory([created]) });
        return created;
      },
      [UPDATE]: remote,
    });
    await user.click(screen.getByRole("button", { name: "Start new draft" }));
    await findToast("Draft saved.");
    expect(backend.callsTo(CREATE)).toHaveLength(1);
    expect(backend.callsTo(CREATE)[0]?.body).toMatchObject({
      level: 4,
      ability_scores: { intelligence: 18 },
    });
    expect(backend.callsTo(UPDATE)).toHaveLength(0);
    await user.clear(score("STR"));
    await user.type(score("STR"), "10");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(backend.callsTo(UPDATE)).toHaveLength(1));
    expect(backend.callsTo(UPDATE)[0]?.params.sheet_version_id).toBe(newId);
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({
      expected_updated_at: createdAt,
    });
  });

  it("refreshes a rejected stale revision without losing the edits or silently resaving", async () => {
    const detail =
      "This sheet changed since you opened it. Review the latest version before saving.";
    const { backend, user } = open({
      [UPDATE]: () => {
        backend.set({ [HISTORY]: playerHistory([remote]) });
        return { status: 409, detail };
      },
    });
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await findToast(detail);
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({
      expected_updated_at: miraDraft.updated_at,
    });
    await waitFor(() => expect(score("INT").value).toBe("18"), {
      timeout: 2_000,
    });
    expect(score("Level").value).toBe("4");
    expect(backend.callsTo(UPDATE)).toHaveLength(1);
    expect(
      screen
        .getByRole("button", { name: "Submit to DM" })
        .hasAttribute("disabled"),
    ).toBe(true);
  });

  it("preserves a still-focused number before blur when another tab submits", async () => {
    const { backend, user } = open();
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    expect(document.activeElement).toBe(level);
    backend.set({
      [HISTORY]: playerHistory([{ ...remote, status: "submitted" }]),
    });
    const buffer = await screen.findByRole<HTMLTextAreaElement>(
      "textbox",
      { name: "Your unsaved sheet changes" },
      { timeout: 6_000 },
    );
    expect(buffer.value).toContain("Level: 4");
    expect(buffer.value).toContain("INT: 18");
    await user.click(
      screen.getByRole("button", { name: "Discard my unsaved changes" }),
    );
    expect(
      screen.queryByRole("region", { name: "Unsaved sheet changes" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Save draft" })).toBeNull();
    expect(backend.callsTo(UPDATE)).toHaveLength(0);
  });
});
