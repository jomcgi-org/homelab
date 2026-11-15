/** The DM's writes to a session's turn order. Reads live in `session.ts`. */
import { api } from "./client";
import type { InitiativeEntry, InitiativeSetRequest } from "./schema";
import type { TurnOrder } from "./session";

/** One entry as the DM stores it. Players receive it masked or not at all. */
export type TurnEntry = Required<InitiativeEntry>;

/** The server caps an order at fifty entries and a label at eighty characters. */
export const MAX_TURN_ENTRIES = 50;
export const MAX_TURN_LABEL = 80;

interface SessionRef {
  campaignId: string;
  sessionId: string;
}

const params = (ref: SessionRef) => ({
  campaign_id: ref.campaignId,
  session_id: ref.sessionId,
});

export const setInitiative = async (
  input: SessionRef & { order: InitiativeSetRequest },
): Promise<TurnOrder> =>
  (await api("PUT /campaigns/{campaign_id}/sessions/{session_id}/initiative", {
    params: params(input),
    body: input.order,
  })) as TurnOrder;

export const advanceInitiative = async (
  input: SessionRef & { direction: "next" | "previous" },
): Promise<TurnOrder> =>
  (await api(
    "POST /campaigns/{campaign_id}/sessions/{session_id}/initiative/advance",
    { params: params(input), body: { direction: input.direction } },
  )) as TurnOrder;

export const endInitiative = async (input: SessionRef): Promise<TurnOrder> =>
  (await api(
    "DELETE /campaigns/{campaign_id}/sessions/{session_id}/initiative",
    { params: params(input) },
  )) as TurnOrder;
