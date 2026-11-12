import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RoadmapItem } from "../../api/schema";
import { paths } from "../../app/paths";
import { openApp } from "../../test/app";
import type { Routes } from "../../test/backend";
import { roadmapIds, roadmapItems } from "../../test/fixtures/feedback";
import { world } from "../../test/world";

/** A roadmap that remembers votes, as the backend does. */
function voting(): Routes {
  let items: RoadmapItem[] = roadmapItems.map((item) => ({ ...item }));
  const set = (id: string, voted: boolean) => {
    items = items.map((item) =>
      item.id === id
        ? { ...item, voted, votes: item.votes + (voted ? 1 : -1) }
        : item,
    );
    const item = items.find((candidate) => candidate.id === id)!;
    return { votes: item.votes, voted };
  };
  return {
    ...world,
    "GET /feedback/roadmap": () => items,
    "PUT /feedback/{feedback_id}/vote": (call) =>
      set(call.params.feedback_id!, true),
    "DELETE /feedback/{feedback_id}/vote": (call) =>
      set(call.params.feedback_id!, false),
  };
}

const panel = () => screen.findByRole("tabpanel");
const row = (scope: HTMLElement, text: RegExp) =>
  within(scope).getByText(text).closest("li")!;

describe("Roadmap", () => {
  it("opens on New, with a count on every tab", async () => {
    openApp(world, paths.roadmap);
    const tabs = within(
      await screen.findByRole("tablist", { name: "Roadmap" }),
    );
    expect(tabs.getAllByRole("tab")).toHaveLength(4);
    for (const name of ["Accepted (1)", "Completed (1)", "Rejected (2)"])
      expect(
        tabs.getByRole("tab", { name }).getAttribute("aria-selected"),
      ).toBe("false");
    expect(
      tabs.getByRole("tab", { name: "New (2)" }).getAttribute("aria-selected"),
    ).toBe("true");
    const list = within(await panel());
    expect(list.getAllByRole("listitem")).toHaveLength(2);
    expect(list.getByText(/which one counted/)).toBeTruthy();
    expect(list.queryByText("Search across campaign notes")).toBeNull();
  });

  it("marks the caller's own item and offers no vote on it", async () => {
    openApp(world, paths.roadmap);
    const mine = row(await panel(), /hard to scan on a phone/);
    expect(within(mine).getByText(/Yours/)).toBeTruthy();
    expect(within(mine).getByText(/1 person agrees/)).toBeTruthy();
    expect(within(mine).queryByRole("button")).toBeNull();
    const other = row(await panel(), /which one counted/);
    expect(within(other).queryByText(/Yours/)).toBeNull();
  });

  it("votes with PUT, withdraws with DELETE, and updates the count", async () => {
    const { user, backend } = openApp(voting(), paths.roadmap);
    const item = row(await panel(), /which one counted/);
    const button = within(item).getByRole("button", { name: "Me too (2)" });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    await user.click(button);
    const pressed = await within(item).findByRole("button", {
      name: "Me too (3)",
    });
    expect(pressed.getAttribute("aria-pressed")).toBe("true");
    expect(
      backend.callsTo("PUT /feedback/{feedback_id}/vote")[0]?.params,
    ).toEqual({ feedback_id: roadmapIds.dice });
    await waitFor(() => expect(pressed).toHaveProperty("disabled", false));
    await user.click(pressed);
    const released = await within(item).findByRole("button", {
      name: "Me too (2)",
    });
    expect(released.getAttribute("aria-pressed")).toBe("false");
    expect(
      backend.callsTo("DELETE /feedback/{feedback_id}/vote")[0]?.params,
    ).toEqual({ feedback_id: roadmapIds.dice });
    expect(backend.callsTo("PUT /feedback/{feedback_id}/vote")).toHaveLength(1);
  });

  it("links an accepted item to its issue and keeps voting open", async () => {
    const { user } = openApp(world, paths.roadmap);
    await user.click(await screen.findByRole("tab", { name: "Accepted (1)" }));
    const item = row(await panel(), /Search across campaign notes/);
    const link = within(item).getByRole("link", { name: /Issue on GitHub/ });
    expect(link.getAttribute("href")).toBe(
      "https://github.example.test/issues/4310",
    );
    expect(
      within(item)
        .getByRole("button", { name: "Me too (3)" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("shows a completed item with its issue and a closed count", async () => {
    const { user } = openApp(world, paths.roadmap);
    await user.click(await screen.findByRole("tab", { name: "Completed (1)" }));
    const item = row(await panel(), /whose turn is next/);
    expect(
      within(item).getByRole("link", { name: /Issue on GitHub/ }),
    ).toBeTruthy();
    expect(within(item).getByText(/4 people agree/)).toBeTruthy();
    expect(within(item).queryByRole("button")).toBeNull();
  });

  it("shows why an item was rejected", async () => {
    const { user } = openApp(world, paths.roadmap);
    await user.click(await screen.findByRole("tab", { name: "Rejected (2)" }));
    const dismissed = row(await panel(), /Let players edit handouts/);
    expect(
      within(dismissed).getByText(/Handouts stay with the DM/),
    ).toBeTruthy();
    expect(within(dismissed).queryByRole("link")).toBeNull();
    expect(within(dismissed).queryByRole("button")).toBeNull();
    // Closed as not planned on GitHub: the issue explains it.
    const closed = row(await panel(), /Ambient sound/);
    expect(
      within(closed).getByRole("link", { name: /Issue on GitHub/ }),
    ).toBeTruthy();
  });

  it("says so when a tab is empty", async () => {
    const { user } = openApp(
      {
        ...world,
        "GET /feedback/roadmap": roadmapItems.filter(
          (item) => item.state === "new",
        ),
      },
      paths.roadmap,
    );
    await user.click(await screen.findByRole("tab", { name: "Completed (0)" }));
    expect(within(await panel()).getByText("Nothing here yet.")).toBeTruthy();
  });

  it("is linked from the lobby for everyone", async () => {
    openApp(world);
    const link = await screen.findByRole("link", { name: "Roadmap" });
    expect(link.getAttribute("href")).toBe("/roadmap");
    expect(screen.queryByRole("link", { name: /Feedback inbox/ })).toBeNull();
  });
});
