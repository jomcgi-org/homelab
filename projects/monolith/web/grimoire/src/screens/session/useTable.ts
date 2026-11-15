import { useQueries } from "@tanstack/react-query";
import { useCharacters, useLobby, useMembers } from "../../api/lobby";
import {
  useEvents,
  useInitiative,
  useSessions,
  useVoices,
  type SessionEvent,
  type TurnOrder,
} from "../../api/session";
import { sheetHistoryOptions } from "../../api/sheets";
import type { GameSessionView, MemberView } from "../../api/schema";
import type { VoicePreset } from "../../voices/voices";
import type { TableCharacter, Viewer } from "./table";

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
    queries: (characters.data ?? []).map((character) =>
      sheetHistoryOptions(campaignId, character.id),
    ),
  });

  const blocking = [
    lobby,
    sessions,
    characters,
    ...(dm ? [members, voices] : []),
    ...(session ? [events, initiative] : []),
  ];
  const failed = blocking.find(
    (query) => query.isError && query.data === undefined,
  );
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
  if (session && (events.data === undefined || initiative.data === undefined))
    return { status: "loading" };
  // Private replies need the member map; restored drafts need the saved voices.
  if (dm && (members.data === undefined || voices.data === undefined))
    return { status: "loading" };

  return {
    status: "ready",
    table: {
      viewer: { user: lobby.data.user, campaign, dm },
      characters: (characters.data ?? []).map((character, index) => ({
        ...character,
        approved:
          sheets[index]?.data?.versions.find(
            (version) => version.status === "approved",
          )?.derived ?? null,
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
