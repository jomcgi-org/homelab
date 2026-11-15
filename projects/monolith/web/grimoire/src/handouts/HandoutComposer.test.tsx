import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { createQueryClient, Providers } from "../app/Providers";
import type { TableCharacter } from "../screens/session/table";
import { findToast } from "../test/app";
import { installBackend, type Backend, type Routes } from "../test/backend";
import { ids } from "../test/fixtures";
import { handoutsRoutes, uploadKey } from "../test/fixtures/handouts";
import { sessionId } from "../test/fixtures/session";
import { HANDOUT_IMAGE_MAX_BYTES } from "../lib/handout.js";
import { HandoutComposer } from "./HandoutComposer";

const pcA = "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1";
const pcB = "b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2";
const character = (id: string, name: string): TableCharacter => ({
  id,
  campaign_id: ids.owned,
  player_name: null,
  character_name: name,
  class_name: null,
  level: 1,
  sheet: {},
  approved: null,
});
const characters = [character(pcA, "Aria Fenn"), character(pcB, "Bram Holt")];

const EVENTS = "POST /campaigns/{campaign_id}/sessions/{session_id}/events";
const UPLOADS = "POST /campaigns/{campaign_id}/handouts/uploads";

let backend: Backend | undefined;
afterEach(() => {
  backend?.restore();
  backend = undefined;
});

function composer(routes: Routes = handoutsRoutes) {
  backend = installBackend(routes);
  render(
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <HandoutComposer
          campaignId={ids.owned}
          sessionId={sessionId}
          characters={characters}
        />
      </Providers>
    </MemoryRouter>,
  );
  return { user: userEvent.setup(), backend };
}

const titleBox = () => screen.getByLabelText(/^Handout title/);
const textBox = () => screen.getByLabelText(/^Handout text/);
const sendButton = () => screen.getByRole("button", { name: "Send handout" });
const fileBox = () =>
  document.querySelector<HTMLInputElement>('input[type="file"]')!;
const png = (name = "pass.png", bytes = 4) =>
  new File([new Uint8Array(bytes)], name, { type: "image/png" });
const sent = (at = 0) =>
  backend!.callsTo(EVENTS)[at]?.body as Record<string, unknown>;

describe("HandoutComposer", () => {
  it("previews the markdown live without script", async () => {
    const { user } = composer();
    const preview = screen.getByRole("region", { name: "Handout preview" });
    expect(
      within(preview).getByRole("heading", { name: "Untitled handout" }),
    ).toBeTruthy();
    await user.type(titleBox(), "Letter");
    await user.type(textBox(), "Hello **you**<script>x()</script>");
    expect(
      within(preview).getByRole("heading", { name: "Letter" }),
    ).toBeTruthy();
    expect(preview.querySelector("strong")?.textContent).toBe("you");
    expect(preview.querySelector("script")).toBeNull();
  });

  it("will not send without a title", async () => {
    const { user, backend } = composer();
    await user.type(textBox(), "Text only");
    await user.click(sendButton());
    expect(backend.callsTo(EVENTS)).toHaveLength(0);
  });

  it("sends a table handout to the session and clears the draft", async () => {
    const { user, backend } = composer();
    await user.type(titleBox(), " Map ");
    await user.type(textBox(), "Text");
    await user.click(sendButton());
    expect(await findToast("Handout sent.")).toBeTruthy();
    const [call] = backend.callsTo(EVENTS);
    expect(call?.params).toEqual({
      campaign_id: ids.owned,
      session_id: sessionId,
    });
    expect(sent()).toMatchObject({
      kind: "handout",
      audience: "table",
      audience_pc_ids: [],
      body: { title: "Map", markdown: "Text" },
    });
    expect(sent().body as object).not.toHaveProperty("image");
    expect(sent().request_id).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() =>
      expect((titleBox() as HTMLInputElement).value).toBe(""),
    );
    expect((textBox() as HTMLTextAreaElement).value).toBe("");
  });

  it("sends to the picked players only", async () => {
    const { user, backend } = composer();
    await user.type(titleBox(), "Secret");
    expect(screen.queryByRole("checkbox", { name: "Bram Holt" })).toBeNull();
    await user.click(
      screen.getByRole("checkbox", { name: "Send to everyone at the table" }),
    );
    // Nobody is chosen yet, so there is nobody to send to.
    await user.click(sendButton());
    expect(backend.callsTo(EVENTS)).toHaveLength(0);
    await user.click(screen.getByRole("checkbox", { name: "Bram Holt" }));
    await user.click(sendButton());
    await waitFor(() => expect(backend.callsTo(EVENTS)).toHaveLength(1));
    expect(sent()).toMatchObject({
      audience: "pcs",
      audience_pc_ids: [pcB],
    });
  });

  it("keeps the draft and reuses the request id when the send fails", async () => {
    const { user, backend } = composer({
      [EVENTS]: { status: 503, detail: "The table is busy." },
    });
    await user.type(titleBox(), "Map");
    await user.click(sendButton());
    expect(await findToast("The table is busy.")).toBeTruthy();
    expect((titleBox() as HTMLInputElement).value).toBe("Map");
    await user.click(sendButton());
    await waitFor(() => expect(backend.callsTo(EVENTS)).toHaveLength(2));
    expect(sent(1).request_id).toBe(sent(0).request_id);
  });

  it("uploads an image without its filename and sends its reference", async () => {
    const { user, backend } = composer();
    await user.type(titleBox(), "Map");
    await user.upload(fileBox(), png());
    // A field status renders twice, like a toast: once for screen readers.
    expect(
      await findToast("Uploaded pass.png (image/png, 2 KB)."),
    ).toBeTruthy();
    const [upload] = backend.callsTo(UPLOADS);
    expect(upload?.params.campaign_id).toBe(ids.owned);
    const file = (upload?.body as FormData).get("file") as File;
    expect(file.size).toBe(4);
    expect(file.name).toBe("handout");
    await user.click(sendButton());
    await waitFor(() => expect(backend.callsTo(EVENTS)).toHaveLength(1));
    expect((sent().body as { image: unknown }).image).toEqual({
      source: "upload",
      key: uploadKey,
    });
    // The key is a reference for the backend, not something to show.
    expect(document.body.innerHTML).not.toContain(uploadKey);
  });

  it("shows the backend refusal when an upload fails and sends no image", async () => {
    const { user, backend } = composer({
      ...handoutsRoutes,
      [UPLOADS]: { status: 415, detail: "unsupported image type" },
    });
    await user.type(titleBox(), "Map");
    await user.upload(fileBox(), png("notes.png"));
    expect(await findToast("unsupported image type")).toBeTruthy();
    expect(screen.queryByText(/^Uploaded /)).toBeNull();
    await user.click(sendButton());
    await waitFor(() => expect(backend.callsTo(EVENTS)).toHaveLength(1));
    expect(sent().body as object).not.toHaveProperty("image");
  });

  it("refuses an oversize image before uploading it", async () => {
    const { user, backend } = composer();
    await user.upload(fileBox(), png("huge.png", HANDOUT_IMAGE_MAX_BYTES + 1));
    expect(
      await findToast("Handout images can be at most 5 MiB."),
    ).toBeTruthy();
    expect(backend.callsTo(UPLOADS)).toHaveLength(0);
  });
});
