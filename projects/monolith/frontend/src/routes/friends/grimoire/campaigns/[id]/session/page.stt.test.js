import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("session transcription status", () => {
  it.each(["degraded", "unavailable", "available", null])(
    "keeps play reachable with provider %s",
    async (provider) => {
      const { body } = render(Page, {
        props: {
          data: {
            campaign: { id: "campaign", name: "Test campaign", role: "player" },
            session: { id: "session", status: "active" },
            transcription: provider ? { provider } : null,
            characters: [],
            events: [],
            voices: [],
            journal: null,
            initiative: null,
          },
        },
      });
      expect(
        body.includes("Transcription unavailable. Play can continue."),
      ).toBe(["degraded", "unavailable"].includes(provider));
      expect(body).toContain("Test campaign");
      expect(body).toContain("textarea");
      if (["degraded", "unavailable"].includes(provider))
        expect(body).toContain('role="status"');
    },
  );
});
