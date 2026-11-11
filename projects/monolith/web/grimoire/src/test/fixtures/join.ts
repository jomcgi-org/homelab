/** Invented join data and the routes that serve it in tests and the preview. */
import type { JoinState } from "../../api/join";
import type { OtherRoutes } from "../backend";

export const joinInvitationId = "77777777-7777-4777-8777-777777777777";

export const joinState = (
  invitation: Partial<JoinState["invitation"]> = {},
  rest: Partial<Omit<JoinState, "invitation">> = {},
): JoinState => ({
  invitation: {
    id: joinInvitationId,
    campaign_id: "88888888-8888-4888-8888-888888888888",
    campaign_name: "The Hollow Lantern",
    invitee_email: "ash@example.test",
    expires_at: "2026-11-18T12:00:00Z",
    status: "pending",
    can_enroll: false,
    ...invitation,
  },
  email: "ash@example.test",
  matches: true,
  ...rest,
});

/** Keyed `METHOD /full/path`: these endpoints sit outside the Grimoire API. */
export const joinOther: OtherRoutes = {
  "GET /grimoire/join/accept/state": joinState(),
  "POST /grimoire/join/accept": { ok: true },
  "POST /grimoire/join": { ok: true },
};
