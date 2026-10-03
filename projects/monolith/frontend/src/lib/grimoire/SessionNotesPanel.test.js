import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import NotesPanel from "./SessionNotesPanel.svelte";

describe("notes editor", () => {
  it("starts on private notes with explicit DM sharing", () => {
    const { body } = render(NotesPanel, {
      props: { endpoint: "/state", showEvent() {} },
    });
    expect(body).toContain("My notes");
    expect(body).toContain("Party notes");
    expect(body).toContain("Only you can read these");
    expect(body).toMatch(/Share this note\s+with\s+the DM/);
    expect(body).not.toContain("checked");
  });
  it("labels the DM's private view without offering player sharing", () => {
    const { body } = render(NotesPanel, {
      props: { endpoint: "/state", dm: true, showEvent() {} },
    });
    expect(body).toContain("Shared with you");
    expect(body).toContain(
      "Character notes players explicitly shared with you",
    );
    expect(body).not.toMatch(/Share this note\s+with\s+the DM/);
  });
});
