import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { openApp } from "../../test/app";
import { ids } from "../../test/fixtures";
import { world } from "../../test/world";

const path = `/campaigns/${ids.owned}/session`;
const sectionNames = ["Story", "Notes", "Inventory", "Journal"];

describe("Session sections", () => {
  it("links every tab to its labelled panel and exposes only the selected panel", async () => {
    const { user } = openApp(world, path);
    const sections = await screen.findByRole("tablist", {
      name: "Session sections",
    });

    for (const name of sectionNames) {
      const tab = within(sections).getByRole("tab", { name });
      const panelId = tab.getAttribute("aria-controls");
      expect(panelId).toBeTruthy();
      const linked = document.getElementById(panelId!);
      expect(linked?.getAttribute("role")).toBe("tabpanel");
      expect(linked?.getAttribute("aria-labelledby")).toBe(tab.id);

      await user.click(tab);

      const panel = await screen.findByRole("tabpanel", { name });
      expect(panel.id).toBe(panelId);
      expect(tab.getAttribute("aria-selected")).toBe("true");
      for (const other of sectionNames.filter((value) => value !== name)) {
        expect(screen.queryByRole("tabpanel", { name: other })).toBeNull();
      }
    }
  });

  it("lets the keyboard select a section and move into its panel", async () => {
    const { user } = openApp(world, path);
    const story = await screen.findByRole("tab", { name: "Story" });
    story.focus();

    await user.keyboard("{ArrowRight}{Enter}");

    const notes = screen.getByRole("tab", { name: "Notes" });
    expect(document.activeElement).toBe(notes);
    expect(notes.getAttribute("aria-selected")).toBe("true");
    const panel = screen.getByRole("tabpanel", { name: "Notes" });
    await user.tab();
    expect(document.activeElement).toBe(panel);

    await user.tab({ shift: true });
    await user.keyboard("{End}{Enter}");
    expect(screen.getByRole("tabpanel", { name: "Journal" })).toBeTruthy();
    await user.keyboard("{Home}{Enter}");
    expect(screen.getByRole("tabpanel", { name: "Story" })).toBeTruthy();
  });

  it("labels the notes audience panel and reaches it from the audience tabs", async () => {
    const { user } = openApp(world, path);
    await user.click(await screen.findByRole("tab", { name: "Notes" }));
    const notes = screen.getByRole("region", { name: "Campaign notes" });
    const audiences = within(notes).getByRole("tablist", {
      name: "Notes audience",
    });
    const audienceTabs = within(audiences).getAllByRole("tab");
    const targets = audienceTabs.map((tab) =>
      tab.getAttribute("aria-controls"),
    );
    expect(new Set(targets).size).toBe(audienceTabs.length);
    for (const tab of audienceTabs) {
      const linked = document.getElementById(
        tab.getAttribute("aria-controls")!,
      );
      expect(linked?.getAttribute("role")).toBe("tabpanel");
      expect(linked?.getAttribute("aria-labelledby")).toBe(tab.id);
    }

    for (const name of ["Shared with you", "Party notes"]) {
      const tab = within(audiences).getByRole("tab", { name });
      await user.click(tab);
      const panel = within(notes).getByRole("tabpanel", { name });
      expect(tab.getAttribute("aria-controls")).toBe(panel.id);
      expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
      expect(within(notes).getAllByRole("tabpanel")).toEqual([panel]);
    }

    await user.click(
      within(audiences).getByRole("tab", { name: "Shared with you" }),
    );
    await user.keyboard("{ArrowRight}{Enter}");
    const panel = within(notes).getByRole("tabpanel", { name: "Party notes" });
    await user.tab();
    expect(document.activeElement).toBe(panel);
  });
});
