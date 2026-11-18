import { screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  keepSheetRecovery,
  restoreSheetRecovery,
  sheetRecoveryEpoch,
} from "../lib/sheet-recovery";
import { findToast, openApp } from "../test/app";
import { lobby } from "../test/fixtures";
import { miraSheet } from "../test/fixtures/sheets";
import { gateway } from "./paths";

const cached = lobby();
const a = { accountId: cached.user.id, campaignId: "table", characterId: "a" };
const b = { ...a, accountId: "current-session-account", characterId: "b" };
const identity = (accountId: string) =>
  new Response(null, {
    status: 204,
    headers: { "X-Grimoire-Viewer-Id": accountId },
  });

function prepare() {
  for (const scope of [a, b])
    keepSheetRecovery(
      scope,
      sheetRecoveryEpoch(scope.accountId),
      Date.now(),
      miraSheet,
      { ...miraSheet, level: 4 },
      {},
      [],
    );
  const assign = vi.fn();
  vi.stubGlobal("location", { origin: "https://friends.example.test", assign });
  return assign;
}
afterEach(() => vi.unstubAllGlobals());

it.each([false, true])(
  "signs out current B without deleting cached A's edits (lobby denied: %s)",
  async (denied) => {
    const assign = prepare();
    const app = openApp(
      { "GET /lobby": denied ? { status: 403 } : cached },
      "/",
      { "HEAD /api/grimoire/lobby/identity": () => identity(b.accountId) },
    );
    await app.user.click(await screen.findByRole("link", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(gateway.signOut));
    expect(restoreSheetRecovery(a, miraSheet)?.sheet.level).toBe(4);
    expect(restoreSheetRecovery(b, miraSheet)).toBeNull();
    expect(app.backend.callsTo("HEAD /api/grimoire/lobby/identity")).toHaveLength(1);
  },
);

it("preserves every account and offers retry when current identity cannot be confirmed", async () => {
  const assign = prepare();
  let confirmed = false;
  const app = openApp({ "GET /lobby": cached }, "/", {
    "HEAD /api/grimoire/lobby/identity": () =>
      confirmed ? identity(b.accountId) : new Response(null, { status: 503 }),
  });
  await app.user.click(await screen.findByRole("link", { name: "Sign out" }));
  await findToast("Could not verify your account for sign-out. Try again.");
  expect(assign).not.toHaveBeenCalled();
  expect(restoreSheetRecovery(a, miraSheet)?.sheet.level).toBe(4);
  expect(restoreSheetRecovery(b, miraSheet)?.sheet.level).toBe(4);
  confirmed = true;
  await app.user.click(screen.getByRole("link", { name: "Sign out" }));
  await waitFor(() => expect(assign).toHaveBeenCalledOnce());
  expect(restoreSheetRecovery(a, miraSheet)?.sheet.level).toBe(4);
  expect(restoreSheetRecovery(b, miraSheet)).toBeNull();
});

it("signs out a confirmed identity with no Grimoire account without clearing other accounts", async () => {
  const assign = prepare();
  const app = openApp({ "GET /lobby": { status: 403 } }, "/", {
    "HEAD /api/grimoire/lobby/identity": () => identity(""),
  });
  await app.user.click(await screen.findByRole("link", { name: "Sign out" }));
  await waitFor(() => expect(assign).toHaveBeenCalledOnce());
  expect(restoreSheetRecovery(a, miraSheet)?.sheet.level).toBe(4);
  expect(restoreSheetRecovery(b, miraSheet)?.sheet.level).toBe(4);
});
