/** Lobby and campaign-membership reads and writes. */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export const keys = {
  lobby: ["lobby"] as const,
  campaign: (id: string) => ["campaign", id] as const,
  members: (id: string) => ["campaign", id, "members"] as const,
  characters: (id: string) => ["campaign", id, "characters"] as const,
  invitations: (id: string) => ["campaign", id, "invitations"] as const,
  joinLinks: (id: string) => ["campaign", id, "join-links"] as const,
};

/** Also the viewer's identity: who is signed in and their role per campaign. */
export const useLobby = () =>
  useQuery({
    queryKey: keys.lobby,
    queryFn: () => api("GET /lobby"),
    refetchInterval: 15_000,
  });

export const useMembers = (campaignId: string, enabled = true) =>
  useQuery({
    queryKey: keys.members(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/members", {
        params: { campaign_id: campaignId },
      }),
    enabled,
  });

export const useCharacters = (campaignId: string, enabled = true) =>
  useQuery({
    queryKey: keys.characters(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/characters", {
        params: { campaign_id: campaignId },
      }),
    enabled,
  });

export const useInvitations = (campaignId: string, enabled = true) =>
  useQuery({
    queryKey: keys.invitations(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/invitations", {
        params: { campaign_id: campaignId },
      }),
    enabled,
  });

/** Single-use links are optional; a failure falls back to email invitations. */
export const useJoinLinks = (campaignId: string, enabled = true) =>
  useQuery({
    queryKey: keys.joinLinks(campaignId),
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/join-links", {
        params: { campaign_id: campaignId },
      }),
    enabled,
    retry: false,
  });

export const createCampaign = (name: string) =>
  api("POST /campaigns", { body: { name } });

export const decideInvitation = (input: {
  invitationId: string;
  decision: "accept" | "decline";
}) =>
  api("POST /invitations/{invitation_id}/{decision}", {
    params: { invitation_id: input.invitationId, decision: input.decision },
  });

export const invitePlayer = (input: { campaignId: string; email: string }) =>
  api("POST /campaigns/{campaign_id}/invitations", {
    params: { campaign_id: input.campaignId },
    body: { email: input.email },
  });

export const cancelInvitation = (input: {
  campaignId: string;
  invitationId: string;
}) =>
  api("DELETE /campaigns/{campaign_id}/invitations/{invitation_id}", {
    params: {
      campaign_id: input.campaignId,
      invitation_id: input.invitationId,
    },
  });

export const removeMember = (input: { campaignId: string; memberId: string }) =>
  api("DELETE /campaigns/{campaign_id}/members/{member_id}", {
    params: { campaign_id: input.campaignId, member_id: input.memberId },
  });

/** Assign an existing character, create and assign one, or clear the seat. */
export type Assignment =
  { player_character_id: string | null } | { new: { name: string } };

export const assignCharacter = (input: {
  campaignId: string;
  memberId: string;
  assignment: Assignment;
}) =>
  api("PUT /campaigns/{campaign_id}/members/{member_id}/character", {
    params: { campaign_id: input.campaignId, member_id: input.memberId },
    body: input.assignment,
  });

export const createOwnCharacter = (input: {
  campaignId: string;
  name: string;
}) =>
  api("POST /campaigns/{campaign_id}/characters/self", {
    params: { campaign_id: input.campaignId },
    body: { name: input.name },
  });

export const createJoinLink = (input: {
  campaignId: string;
  email: string;
  allowEnrollment: boolean;
}) =>
  api("POST /campaigns/{campaign_id}/join-links", {
    params: { campaign_id: input.campaignId },
    body: { email: input.email, allow_enrollment: input.allowEnrollment },
  });

export const revokeJoinLink = (input: { campaignId: string; linkId: string }) =>
  api("DELETE /campaigns/{campaign_id}/join-links/{link_id}", {
    params: { campaign_id: input.campaignId, link_id: input.linkId },
  });
