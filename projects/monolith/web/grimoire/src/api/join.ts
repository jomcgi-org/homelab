/**
 * The signed-in half of a single-use campaign link. The link's token stays in
 * an HttpOnly cookie the backend reads; the browser only ever names the
 * invitation by id. These endpoints sit beside the anonymous landing, outside
 * the Grimoire API.
 */
import { useQuery } from "@tanstack/react-query";
import { requestJson } from "./client";

export interface JoinInvitation {
  id: string;
  campaign_id: string;
  campaign_name: string;
  invitee_email: string;
  expires_at: string;
  /** "pending" until accepted, revoked or expired. */
  status: string;
  can_enroll: boolean;
  enrollment_cleanup_pending?: boolean;
}

export interface JoinState {
  invitation: JoinInvitation;
  /** The signed-in account's email. */
  email: string;
  /** Whether that email is the invited one. Advisory: accept checks identity. */
  matches: boolean;
}

export const joinKeys = {
  state: ["join", "accept"] as const,
};

/** Never served from cache: an invitation can be used or revoked elsewhere. */
export const useJoinState = () =>
  useQuery({
    queryKey: joinKeys.state,
    queryFn: ({ signal }) =>
      requestJson<JoinState>("/grimoire/join/accept/state", { signal }),
    staleTime: 0,
    gcTime: 0,
  });

/** Naming the invitation stops an older tab accepting a newer link. */
export const acceptJoin = (invitationId: string) =>
  requestJson<{ ok: true }>("/grimoire/join/accept", {
    method: "POST",
    body: { invitation_id: invitationId },
  });

/** Forgets the invitation in this browser. The link itself stays valid. */
export const closeJoin = () =>
  requestJson<{ ok: true }>("/grimoire/join", {
    method: "POST",
    body: { action: "cancel" },
  });
