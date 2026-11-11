import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { SessionEventView } from "../api/schema";
import { createQueryClient, Providers } from "../app/Providers";
import { ids } from "../test/fixtures";
import { mapHandout, uploadKey } from "../test/fixtures/handouts";
import { sessionId } from "../test/fixtures/session";
import { HandoutCard, type HandoutCardProps } from "./HandoutCard";

const entityId = "f2000000-0000-4000-8000-000000000001";

function card(props: Partial<HandoutCardProps> = {}) {
  const onPin = vi.fn();
  const onOpenKnowledge = vi.fn();
  const handout = props.handout ?? mapHandout();
  const { container } = render(
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <HandoutCard
          campaignId={ids.owned}
          sessionId={sessionId}
          dm={false}
          onPin={onPin}
          onOpenKnowledge={onOpenKnowledge}
          {...props}
          handout={handout}
        />
      </Providers>
    </MemoryRouter>,
  );
  return { container, handout, onPin, onOpenKnowledge };
}

describe("HandoutCard", () => {
  it("renders the title and markdown", () => {
    const { container } = card();
    expect(
      screen.getByRole("heading", { name: "A map of the pass" }),
    ).toBeTruthy();
    expect(container.querySelector("strong")?.textContent).toBe("ice");
  });

  it("never lets a script tag into the DOM", () => {
    const { container } = card();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("serves the image through the audience-checked route, never the storage key", () => {
    const { container, handout } = card();
    const image = screen.getByRole("img", {
      name: "Handout image: A map of the pass",
    });
    expect(image.getAttribute("src")).toBe(
      `/api/grimoire/campaigns/${ids.owned}/sessions/${sessionId}/events/${handout.id}/image`,
    );
    expect(container.innerHTML).not.toContain(uploadKey);
  });

  it("renders no image when the handout has none", () => {
    card({
      handout: mapHandout({ body: { title: "Note", markdown: "Text" } }),
    });
    expect(screen.getByRole("heading", { name: "Note" })).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("falls back to a plain title", () => {
    card({ handout: mapHandout({ body: { markdown: "Text" } }) });
    expect(screen.getByRole("heading", { name: "Handout" })).toBeTruthy();
  });

  it("offers a player the pin action and pins that handout", async () => {
    const { handout, onPin } = card();
    const pin = screen.getByRole("button", {
      name: "Pin A map of the pass to my notes",
    });
    expect(pin.textContent).toBe("Pin to my notes");
    await userEvent.click(pin);
    expect(onPin).toHaveBeenCalledTimes(1);
    expect((onPin.mock.calls[0]![0] as SessionEventView).id).toBe(handout.id);
  });

  it("offers the DM no pin action", () => {
    card({ dm: true });
    expect(screen.queryByRole("button")).toBeNull();
    expect(
      screen.getByRole("heading", { name: "A map of the pass" }),
    ).toBeTruthy();
  });

  it("opens the linked knowledge only when the handout carries a link", async () => {
    const { onOpenKnowledge } = card({
      handout: mapHandout({
        body: { title: "Letter", markdown: "", entity_id: entityId },
      }),
    });
    await userEvent.click(
      screen.getByRole("button", {
        name: "Explore knowledge linked to Letter",
      }),
    );
    expect(onOpenKnowledge).toHaveBeenCalledWith(entityId);
  });

  it.each([
    ["a retraction time", { retracted_at: "2026-11-10T19:10:00Z" }],
    ["no body, as a player receives it", { body: null as never }],
  ])(
    "renders a retracted handout (%s) without its body, image or pin",
    (_name, overrides) => {
      const { container } = card({ handout: mapHandout(overrides) });
      expect(container.textContent).toContain("This handout was retracted.");
      expect(container.textContent).not.toContain("A map of the pass");
      expect(container.textContent).not.toContain("ice");
      expect(screen.queryByRole("img")).toBeNull();
      expect(screen.queryByRole("button")).toBeNull();
    },
  );
});
