/** Invented handouts data and the routes that serve it in tests and the preview. */
import type { HandoutUploadView, SessionEventView } from "../../api/schema";
import type { Routes } from "../backend";
import { ids } from "../fixtures";
import { event } from "./session";

/** A storage key as the backend mints it. It must never reach the page. */
export const uploadKey = `campaigns/${ids.owned}/handouts/${"a".repeat(32)}.png`;

export const upload: HandoutUploadView = {
  key: uploadKey,
  content_type: "image/png",
  size: 2048,
};

/** A handout to the table. Its text carries markup that must stay inert. */
export const mapHandout = (
  overrides: Partial<SessionEventView> = {},
): SessionEventView =>
  event({
    kind: "handout",
    body: {
      title: "A map of the pass",
      markdown: "Mind the **ice**.\n\n<script>alert(1)</script>",
      image: { source: "upload", key: uploadKey },
    },
    ...overrides,
  });

export const handoutsRoutes: Routes = {
  "POST /campaigns/{campaign_id}/handouts/uploads": upload,
  "POST /campaigns/{campaign_id}/sessions/{session_id}/events": (call) => {
    const sent = call.body as Pick<
      SessionEventView,
      "kind" | "audience" | "audience_pc_ids" | "body"
    >;
    return event({
      kind: sent.kind,
      audience: sent.audience,
      audience_pc_ids: sent.audience_pc_ids,
      body: sent.body,
    });
  },
};
