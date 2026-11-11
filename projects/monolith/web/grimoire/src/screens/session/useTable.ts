import { useQueries } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useCharacters, useLobby, useMembers } from "../../api/lobby";
import {
  sessionKeys,
  useEvents,
  useInitiative,
  useSessions,
  useVoices,
  type ApprovedSheet,
  type SessionEvent,
  type TurnOrder,
} from "../../api/session";
import type { GameSessionView, MemberView } from "../../api/schema";
import type { VoicePreset } from "../../voices/voices";
import type { TableCharacter, Viewer } from "./table";

const SLOW_MS = 15_000;

export interface Table {
  viewer: Viewer;
  characters: TableCharacter[];
  sessions: GameSessionView[];
  /** The selected session, else the most recent, else null before the first. */
  session: GameSessionView | null;
  events: SessionEvent[];
  /** The caller's view of the turn order. */
  initiative: TurnOrder;
  voices: VoicePreset[];
  /** DM only. */
  members: MemberView[];
  /** False while the feed's last poll failed. */
  feedLive: boolean;
}

export type TableState =
  | { status: "loading" }
  | { status: "error"; error: unknown; retry: () => void }
  | { status: "not-member" }
  | { status: "session-missing" }
  | { status: "ready"; table: Table };

/**
 * Everything the session screen shows, assembled from independent queries so
 * each refreshes at its own pace (see api/session.ts).
 */
export function useTable(
  campaignId: string,
  selectedSessionId: string | null,
): TableState {
  const lobby = useLobby();
  const campaign = lobby.data?.campaigns.find((row) => row.id === campaignId);
  const member = campaign !== undefined;
  const dm = campaign?.role === "dm";
  const sessions = useSessions(campaignId);
  const characters = useCharacters(campaignId, member);
  const members = useMembers(campaignId, dm);
  const voices = useVoices(campaignId);
  const session =
    (selectedSessionId
      ? sessions.data?.find((row) => row.id === selectedSessionId)
      : sessions.data?.[0]) ?? null;
  const events = useEvents(campaignId, session);
  const initiative = useInitiative(campaignId, session);
  const sheets = useQueries({
    queries: (characters.data ?? []).map((character) => ({
      queryKey: sessionKeys.sheets(campaignId, character.id),
      queryFn: async (): Promise<ApprovedSheet | null> => {
        const history = await api(
          "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets",
          {
            params: {
              campaign_id: campaignId,
              player_character_id: character.id,
            },
          },
        );
        const approved = history.versions
          .filter((version) => version.status === "approved")
          .sort((a, b) => b.version - a.version)[0];
        return (approved?.derived as ApprovedSheet | undefined) ?? null;
      },
      refetchInterval: SLOW_MS * 4,
    })),
  });

  const blocking = [lobby, sessions, characters];
  const failed = blocking.find((query) => query.isError && !query.data);
  if (failed)
    return {
      status: "error",
      error: failed.error,
      retry: () => blocking.forEach((query) => query.refetch()),
    };
  if (!lobby.data || !sessions.data || (member && !characters.data))
    return { status: "loading" };
  if (!campaign) return { status: "not-member" };
  if (selectedSessionId && !session) return { status: "session-missing" };
  if (session && events.data === undefined) return { status: "loading" };

  return {
    status: "ready",
    table: {
      viewer: { user: lobby.data.user, campaign, dm },
      characters: (characters.data ?? []).map((character, index) => ({
        ...character,
        approved: sheets[index]?.data ?? null,
      })),
      sessions: sessions.data,
      session,
      events: events.data ?? [],
      initiative: initiative.data ?? null,
      voices: (voices.data ?? []) as VoicePreset[],
      members: members.data ?? [],
      feedLive: !events.isError,
    },
  };
}
