/** Campaign notes. The notes panel and screen add their reads and writes here. */
import { api } from "./client";

export const noteKeys = {
  all: (campaignId: string) => ["campaign", campaignId, "notes"] as const,
};

/** Copy a feed event into the viewer's character notes. */
export const pinEvent = (input: { campaignId: string; eventId: string }) =>
  api("POST /campaigns/{campaign_id}/notes", {
    params: { campaign_id: input.campaignId },
    body: { kind: "character", from_event_id: input.eventId },
  });
