import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { keys } from "../../api/lobby";
import { openApp } from "../../test/app";
import { campaigns, characters, ids, lobby } from "../../test/fixtures";
import { note } from "../../test/fixtures/notes";
import { sessionRoutes } from "../../test/fixtures/session";

afterEach(() => vi.unstubAllGlobals());

it("removes a player's private notes before an uncached DM member read can remount the table", async () => {
  const reload = vi.fn();
  vi.stubGlobal("location", { origin: "https://friends.example.test", reload });
  const privateNote = note({
    id: "77777777-7777-4777-8777-777777777777",
    title: "A player-only secret",
    markdown: "The DM has never been granted this note.",
    dm_readable: false,
  });
  const player = lobby({
    campaigns: [
      {
        ...campaigns.owned,
        role: "player",
        is_owner: false,
        player_character_id: ids.spare,
      },
    ],
  });
  const app = openApp(
    {
      ...sessionRoutes,
      "GET /lobby": player,
      "GET /campaigns/{campaign_id}/characters": characters,
      "GET /campaigns/{campaign_id}/notes": [privateNote],
    },
    `/campaigns/${ids.owned}/session`,
  );
  await app.user.click(await screen.findByRole("tab", { name: "Notes" }));
  await screen.findByRole("heading", { name: privateNote.title });
  expect(
    app.backend.callsTo("GET /campaigns/{campaign_id}/members"),
  ).toHaveLength(0);

  // The new DM has not loaded this campaign's members yet. Hold that read,
  // and refuse the notes refresh so stale player notes cannot be masked by
  // a fast successful response for the new account.
  const fetch = globalThis.fetch;
  let releaseMembers: (() => void) | undefined;
  globalThis.fetch = (input, init) =>
    String(input).endsWith("/members")
      ? new Promise((resolve) => {
          releaseMembers = () => resolve(fetch(input, init));
        })
      : fetch(input, init);
  app.backend.set({
    "GET /lobby": lobby({
      user: {
        ...player.user,
        id: "88888888-8888-4888-8888-888888888888",
        display_name: "The new DM",
      },
    }),
    "GET /campaigns/{campaign_id}/notes": {
      status: 503,
      detail: "Notes unavailable.",
    },
  });
  await act(async () => {
    await app.client.invalidateQueries({ queryKey: keys.lobby });
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  await waitFor(() =>
    expect(reload.mock.calls.length > 0 || releaseMembers !== undefined).toBe(
      true,
    ),
  );
  await act(async () => {
    releaseMembers?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  await waitFor(() =>
    expect(
      reload.mock.calls.length > 0 ||
        screen.queryByText("Dungeon master") !== null,
    ).toBe(true),
  );
  const notes = screen.queryByRole("tab", { name: "Notes" });
  if (notes) await app.user.click(notes);

  expect(screen.queryByRole("heading", { name: privateNote.title })).toBeNull();
  expect(app.client.getQueryCache().getAll()).toHaveLength(0);
  expect(reload).toHaveBeenCalledTimes(1);
});
