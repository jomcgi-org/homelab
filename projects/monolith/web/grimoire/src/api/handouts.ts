/** Handouts: the image upload, the image URL and the event a DM posts. */
import { composeHandout, handoutEvent } from "../lib/handout.js";
import { api, apiUrl, type Body } from "./client";
import type { SessionEventRequest } from "./schema";
import type { SessionEvent } from "./session";

/** What a handout event carries. `image` is present only as a flag to fetch. */
export interface HandoutBody {
  title?: string;
  markdown?: string;
  entity_id?: string;
  image?: object;
  retracted?: boolean;
}

/** A player receives no body once the handout is retracted. */
export const handoutBody = (event: SessionEvent): HandoutBody | null =>
  (event.body as HandoutBody | null) ?? null;

/**
 * The only way the UI refers to a handout image. The backend applies the
 * event's audience to every request, so the stored object key is never used.
 */
export const handoutImageUrl = (
  campaignId: string,
  sessionId: string,
  eventId: string,
) =>
  apiUrl(
    "GET /campaigns/{campaign_id}/sessions/{session_id}/events/{event_id}/image",
    {
      params: {
        campaign_id: campaignId,
        session_id: sessionId,
        event_id: eventId,
      },
    },
  );

type UploadBody = Body<"POST /campaigns/{campaign_id}/handouts/uploads">;

/** Stores an image for a handout. The backend sniffs the real type and size. */
export const uploadHandoutImage = (input: {
  campaignId: string;
  file: File;
}) => {
  const form = new FormData();
  // The DM's filename stays on their machine.
  form.set(
    "file",
    new File([input.file], "handout", { type: input.file.type }),
  );
  return api("POST /campaigns/{campaign_id}/handouts/uploads", {
    params: { campaign_id: input.campaignId },
    // The schema describes the multipart field; the client sends the form.
    body: form as unknown as UploadBody,
  });
};

export interface HandoutDraft {
  title: string;
  markdown: string;
  /** Character ids, or "table" for everyone. */
  audience: "table" | string[];
  imageKey: string | null;
  requestId: string;
}

/** The event for a draft. Throws an Error whose message names what to fix. */
export function handoutEventFor(draft: HandoutDraft): SessionEventRequest {
  const message = composeHandout({
    title: draft.title,
    markdown: draft.markdown,
    audience:
      draft.audience === "table"
        ? "table"
        : draft.audience.map((id) => `pc:${id}`),
    // The composer has no knowledge picker; a handout links none.
    entityId: undefined,
    image: draft.imageKey ? { source: "upload", key: draft.imageKey } : null,
  });
  return handoutEvent({
    ...message,
    requestId: draft.requestId,
  }) as SessionEventRequest;
}
