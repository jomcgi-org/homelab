/**
 * Platform accounts: who may sign in, what each account may open, and the
 * invitations that create accounts. These endpoints sit outside the Grimoire
 * API. Every change carries a reason for the audit trail and a request id
 * that makes a retry safe.
 */
import { useQuery } from "@tanstack/react-query";
import { ApiError, requestJson } from "./client";

const ROOT = "/api/auth/platform";
const INVITATION_ID = /^[0-9a-f-]{36}$/i;
/** How long a new invitation lasts. The backend allows one to seven days. */
const INVITATION_DAYS = 7;

export interface PlatformIdentity {
  issuer: string;
  subject: string;
}

export interface PlatformUser {
  id: string;
  username: string;
  email: string | null;
  display_name: string;
  active: boolean;
  identities: PlatformIdentity[];
  /** Granted permission names, sorted. */
  permissions: string[];
}

export type InvitationStatus =
  "awaiting_delivery" | "pending" | "accepted" | "revoked" | "expired";

export interface PlatformInvitation {
  id: string;
  recipient_label: string;
  status: InvitationStatus;
  expires_at: string;
  /** Where an operator opens this invitation to show its link. */
  delivery_reference: string;
}

export interface PlatformPermission {
  name: string;
  description: string;
}

interface Listing<T> {
  items: T[];
  /** The id to continue after, or null on the last page. */
  next: string | null;
}

export interface Directory {
  users: Listing<PlatformUser>;
  invitations: Listing<PlatformInvitation>;
  permissions: PlatformPermission[];
  /** The invitation opened by id does not exist. */
  selectedMissing: boolean;
}

type Audited<T> = T & { audit_reference: string };

/** The link's secret is returned once and never stored by the backend. */
export type DeliveredInvitation = Audited<PlatformInvitation> & {
  token: string;
};

/**
 * One management change, as the operator states it. `deliver` shows an
 * invitation's link for the first time; `reissue` replaces a link that was
 * already shown, which stops the old one working.
 */
export type Command = { reason: string } & (
  | { action: "bootstrap"; user_id?: string }
  | { action: "issue"; recipient_label: string }
  | { action: "revoke_invitation"; invitation_id: string }
  | { action: "deliver"; invitation_id: string }
  | { action: "reissue"; invitation_id: string }
  | { action: "set_active"; user_id: string; active: boolean }
  | { action: "grant"; user_id: string; permission: string }
  | { action: "revoke_grant"; user_id: string; permission: string }
);

export type CommandResult =
  Audited<PlatformUser> | Audited<PlatformInvitation> | DeliveredInvitation;

/** A change plus the id that lets the backend recognise a retry of it. */
export type Keyed<T> = T & { request_id: string };

export const platformKeys = {
  all: ["platform"] as const,
  directory: (selectedInvitation: string | null) =>
    ["platform", "directory", selectedInvitation] as const,
  self: ["platform", "self"] as const,
};

const read = <T>(path: string, signal: AbortSignal) =>
  requestJson<T>(`${ROOT}${path}`, { signal });

/**
 * The first page of users and invitations with the permission registry. An
 * invitation opened by id is added when it is beyond that first page.
 */
async function directory(
  selectedInvitation: string | null,
  signal: AbortSignal,
): Promise<Directory> {
  const [users, invitations, permissions] = await Promise.all([
    read<Listing<PlatformUser>>("/management/users", signal),
    read<Listing<PlatformInvitation>>("/management/invitations", signal),
    read<PlatformPermission[]>("/management/permissions", signal),
  ]);
  const listed = { users, invitations, permissions, selectedMissing: false };
  if (
    !selectedInvitation ||
    invitations.items.some((row) => row.id === selectedInvitation)
  )
    return listed;
  try {
    const selected = await read<PlatformInvitation>(
      `/management/invitation?invitation_id=${encodeURIComponent(selectedInvitation)}`,
      signal,
    );
    return {
      ...listed,
      invitations: { ...invitations, items: [selected, ...invitations.items] },
    };
  } catch (error) {
    // A stale or mistyped link must not hide the accounts that did load.
    if (error instanceof ApiError && [400, 404].includes(error.status))
      return { ...listed, selectedMissing: true };
    throw error;
  }
}

export const useDirectory = (selectedInvitation: string | null) =>
  useQuery({
    queryKey: platformKeys.directory(selectedInvitation),
    queryFn: ({ signal }) => directory(selectedInvitation, signal),
  });

export const useOwnAccount = () =>
  useQuery({
    queryKey: platformKeys.self,
    queryFn: ({ signal }) => read<PlatformUser>("/self", signal),
  });

function deliver(
  command: Keyed<Extract<Command, { action: "deliver" | "reissue" }>>,
): Promise<DeliveredInvitation> {
  if (!INVITATION_ID.test(command.invitation_id))
    throw new ApiError(400, "Invalid invitation.");
  return requestJson<DeliveredInvitation>(
    `${ROOT}/invitations/${command.invitation_id}/deliver`,
    {
      method: "POST",
      body: {
        request_id: command.request_id,
        reason: command.reason,
        reissue: command.action === "reissue",
      },
    },
  );
}

function commandArguments(
  command: Exclude<Command, { action: "deliver" | "reissue" }>,
): Record<string, unknown> {
  switch (command.action) {
    case "bootstrap":
      return command.user_id ? { user_id: command.user_id } : {};
    case "issue":
      return {
        recipient_label: command.recipient_label,
        expires_in_days: INVITATION_DAYS,
      };
    case "revoke_invitation":
      return { invitation_id: command.invitation_id };
    case "set_active":
      return { user_id: command.user_id, active: command.active };
    case "grant":
    case "revoke_grant":
      return { user_id: command.user_id, permission: command.permission };
  }
}

export async function sendCommand(
  command: Keyed<Command>,
): Promise<CommandResult> {
  if (command.action === "deliver" || command.action === "reissue")
    return deliver(command);
  return requestJson<CommandResult>(`${ROOT}/commands`, {
    method: "POST",
    body: {
      action: command.action,
      request_id: command.request_id,
      reason: command.reason,
      arguments: commandArguments(command),
    },
  });
}
