import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../../api/lobby";
import { keys as sheetKeys } from "../../api/sheets";
import { signIn } from "../../app/signIn";
import { openApp } from "../../test/app";
import type { Routes } from "../../test/backend";
import { campaigns, ids, lobby } from "../../test/fixtures";
import { mira, miraDraft, miraSheet, playerHistory, sheetsRoutes } from "../../test/fixtures/sheets";

const SHEETS = "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets";
const CHARACTERS = "GET /campaigns/{campaign_id}/characters";
const viewer = lobby({ campaigns: [campaigns.joined] });
const history = playerHistory([{ ...miraDraft, decision_comment: "PRIVATE_SHEET_CANARY" }]);
const routes: Routes = { ...sheetsRoutes, "GET /lobby": viewer, [SHEETS]: history };
const level = () => screen.getByRole<HTMLInputElement>("spinbutton", { name: "Level" });
const remount = (app: ReturnType<typeof openApp>, next: Routes = routes) => {
  cleanup(); app.client.clear(); app.backend.restore();
  return openApp(next, "/sheets");
};
async function dirty() {
  const app = openApp(routes, "/sheets");
  await screen.findByRole("spinbutton", { name: "Level" });
  await app.user.clear(level());
  await app.user.type(level(), "4");
  return app;
}
afterEach(() => vi.unstubAllGlobals());

describe("authorized sheet recovery", () => {
  it.each(["clear", "swap", "membership", "account"])("hides a dirty editor after %s and recovers only the original account and character", async (change) => {
    const reload = vi.fn();
    vi.stubGlobal("location", { origin: "https://friends.example.test", reload });
    let app = await dirty();
    const next = change === "membership" ? { ...viewer, campaigns: [] }
      : change === "account" ? { ...viewer, user: { ...viewer.user, id: "other-account" } }
      : { ...viewer, campaigns: [{ ...campaigns.joined, player_character_id: change === "clear" ? null : ids.spare }] };
    app.backend.set({ "GET /lobby": next, [SHEETS]: { status: 404 } });
    await act(async () => { await app.client.invalidateQueries({ queryKey: keys.lobby }); });
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    expect(screen.queryByText("PRIVATE_SHEET_CANARY")).toBeNull();
    expect(screen.queryByRole("region", { name: /Recovered|Unsaved/ })).toBeNull();
    expect(app.client.getQueryCache().getAll()).toHaveLength(0);

    // Even an account currently authorized for the SAME character gets none
    // of the former account's local values.
    app = remount(app, { ...routes, "GET /lobby": { ...viewer, user: { ...viewer.user, id: "other-account" } } });
    await screen.findByRole("spinbutton", { name: "Level" });
    expect(level().value).toBe("3");
    app = remount(app, {
      ...routes, "GET /lobby": { ...viewer, campaigns: [{ ...campaigns.joined, player_character_id: ids.spare }] },
      [CHARACTERS]: [{ ...mira, id: ids.spare, character_name: "New character" }],
      [SHEETS]: { ...history, character: { ...mira, id: ids.spare, character_name: "New character" } },
    });
    await screen.findByRole("spinbutton", { name: "Level" });
    expect(level().value).toBe("3");
    app = remount(app);
    await screen.findByRole("region", { name: "Recovered sheet changes" });
    expect(level().value).toBe("4");
    expect(app.backend.callsTo(SHEETS)).toHaveLength(1);
    expect(app.backend.calls.some((call) => /^(POST|PATCH)/.test(call.operation))).toBe(false);
  });

  it("waits for a fresh authorized read even with a cached sheet and preserves the buffer on a refusal", async () => {
    const app = await dirty();
    await app.user.click(screen.getByRole("link", { name: "Your campaigns" }));
    app.backend.set({ [SHEETS]: { status: 503, detail: "Try later" } });
    await app.user.click(screen.getByRole("link", { name: "Character sheets" }));
    await screen.findByText("Try later");
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Recovered sheet changes" })).toBeNull();
    app.backend.set({ [SHEETS]: playerHistory([{ ...miraDraft, sheet: { ...miraSheet, level: 5 } }]) });
    await app.user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("region", { name: "Resolve sheet changes" });
    expect(level().value).toBe("4");
    expect(screen.getByRole("button", { name: "Save draft" }).hasAttribute("disabled")).toBe(true);
    await app.user.click(screen.getByRole("button", { name: "Discard recovered changes" }));
    expect(level().value).toBe("5");
    expect(Object.keys(localStorage).some((key) => key.startsWith("grimoire-sheet-recovery:"))).toBe(false);
  });

  it("fences a refused sheet poll even before the lobby confirms the reassignment", async () => {
    vi.stubGlobal("location", { origin: "https://friends.example.test", reload: vi.fn() });
    const app = await dirty();
    app.backend.set({ [SHEETS]: { status: 404 } });
    await act(async () => { await app.client.invalidateQueries({ queryKey: sheetKeys.sheets(ids.joined, ids.mira) }); });
    await waitFor(() => expect(screen.queryByText("PRIVATE_SHEET_CANARY")).toBeNull());
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
  });

  it("keeps recovery through expiry, then clears it on the real sign-out link", async () => {
    let app = await dirty();
    act(() => signIn.signal());
    app = remount(app);
    await screen.findByRole("region", { name: "Recovered sheet changes" });
    expect(level().value).toBe("4");
    await app.user.click(screen.getByRole("link", { name: "Your campaigns" }));
    await app.user.click(screen.getByRole("link", { name: "Sign out" }));
    expect(Object.keys(localStorage).some((key) => key.startsWith("grimoire-sheet-recovery:"))).toBe(false);
    app = remount(app);
    await screen.findByRole("spinbutton", { name: "Level" });
    expect(level().value).toBe("3");
  });

  it("restores a submitted sheet only as a read-only buffer until the DM returns it", async () => {
    let app = await dirty();
    app = remount(app, { ...routes, [SHEETS]: playerHistory([{ ...miraDraft, status: "submitted" }]) });
    const buffer = await screen.findByRole<HTMLTextAreaElement>("textbox", { name: "Your unsaved sheet changes" });
    expect(buffer.readOnly).toBe(true);
    expect(buffer.value).toContain("Level: 4");
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save draft" })).toBeNull();
    app.backend.set({ [SHEETS]: playerHistory([{ ...miraDraft, status: "returned" }]) });
    await act(async () => { await app.client.invalidateQueries({ queryKey: sheetKeys.sheets(ids.joined, ids.mira) }); });
    await screen.findByRole("spinbutton", { name: "Level" });
    expect(level().value).toBe("4");
    expect(screen.getByRole("button", { name: "Start new draft" })).toBeTruthy();
  });
});
