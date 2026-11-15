import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { findToast, openApp } from "../../test/app";
import { campaigns, characters, ids, lobby } from "../../test/fixtures";
import { note } from "../../test/fixtures/notes";
import { event, sessionRoutes, story } from "../../test/fixtures/session";

describe("pending feed pins", () => {
  it.each(["success", "failure"] as const)(
    "disables every pin until %s and then allows a different event",
    async (outcome) => {
      const handout = event({
        kind: "handout",
        body: { title: "A sealed letter", markdown: "Meet at the bridge." },
      });
      const entries = [...story, handout];
      const pinned = note({
        id: "77777777-7777-4777-8777-777777777777",
        title: "Pinned message",
      });
      const operation = "POST /campaigns/{campaign_id}/notes";
      const { user, backend } = openApp(
        {
          ...sessionRoutes,
          "GET /lobby": lobby({
            campaigns: [
              {
                ...campaigns.owned,
                role: "player",
                is_owner: false,
                player_character_id: ids.spare,
              },
            ],
          }),
          "GET /campaigns/{campaign_id}/characters": characters,
          "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
            entries.filter(
              (row) => row.seq > Number(call.query.get("after") ?? 0),
            ),
          [operation]:
            outcome === "success"
              ? pinned
              : { status: 503, detail: "Pinning is unavailable." },
        },
        `/campaigns/${ids.owned}/session`,
      );
      const first = await screen.findByRole(
        "button",
        {
          name: "Pin Rain hammers the shutters of the Gilded Eel. to notes",
        },
        { timeout: 5000 },
      );
      const next = screen.getByRole("button", {
        name: "Pin A sealed letter to my notes",
      });
      const fetch = globalThis.fetch;
      let release!: () => void;
      const held = new Promise<void>((resolve) => {
        release = resolve;
      });
      globalThis.fetch = async (input, init) => {
        const response = await fetch(input, init);
        if (init?.method === "POST") await held;
        return response;
      };
      try {
        await user.click(first);
        await waitFor(() => expect(backend.callsTo(operation)).toHaveLength(1));
        await waitFor(() => {
          for (const button of screen.getAllByRole("button", { name: /^Pin / }))
            expect(button.hasAttribute("disabled")).toBe(true);
        });
        await user.click(next);
        expect(backend.callsTo(operation)).toHaveLength(1);
      } finally {
        await act(async () => {
          release();
        });
      }

      await findToast(
        outcome === "success"
          ? "Pinned to your notes."
          : "Pinning is unavailable.",
      );
      if (outcome === "success")
        await user.click(screen.getByRole("tab", { name: "Story" }));
      await waitFor(() => {
        for (const button of screen.getAllByRole("button", { name: /^Pin / }))
          expect(button.hasAttribute("disabled")).toBe(false);
      });
      backend.set({ [operation]: pinned });
      await user.click(
        screen.getByRole("button", { name: "Pin A sealed letter to my notes" }),
      );
      await findToast("Pinned to your notes.");
      expect(backend.callsTo(operation).map((call) => call.body)).toEqual([
        { kind: "character", from_event_id: story[0]!.id },
        { kind: "character", from_event_id: handout.id },
      ]);
    },
  );
});
