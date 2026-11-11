/** Invented platform data and the routes that serve it in tests and the preview. */
import type {
  DeliveredInvitation,
  PlatformInvitation,
  PlatformPermission,
  PlatformUser,
} from "../../api/platform";
import type { Call, OtherRoutes } from "../backend";

export const platformIds = {
  operator: "a1111111-1111-4111-8111-111111111111",
  friend: "a2222222-2222-4222-8222-222222222222",
  disabled: "a3333333-3333-4333-8333-333333333333",
  unshown: "b1111111-1111-4111-8111-111111111111",
  shown: "b2222222-2222-4222-8222-222222222222",
  accepted: "b3333333-3333-4333-8333-333333333333",
  beyondFirstPage: "b9999999-9999-4999-8999-999999999999",
};

/** 43 characters, the length of a real invitation token. */
export const platformToken = "Zm9yLXRlc3RzLW9ubHktbm90LWEtcmVhbC10b2tlbi0";

export const platformPermissions: PlatformPermission[] = [
  {
    name: "grimoire.access",
    description: "Enter Grimoire; campaign membership is checked separately.",
  },
  {
    name: "grimoire.create_game",
    description: "Create a campaign; does not grant access to other campaigns.",
  },
];

export const platformUser = (
  overrides: Partial<PlatformUser> = {},
): PlatformUser => ({
  id: platformIds.friend,
  username: "wren",
  email: null,
  display_name: "Wren Alder",
  active: true,
  identities: [
    { issuer: "https://auth.example.test/o/grimoire/", subject: "wren" },
  ],
  permissions: ["grimoire.access"],
  ...overrides,
});

export const platformUsers: PlatformUser[] = [
  platformUser({
    id: platformIds.operator,
    username: "marlow",
    email: "marlow@example.test",
    display_name: "Marlow Finch",
    permissions: ["grimoire.access", "grimoire.create_game"],
  }),
  platformUser(),
  platformUser({
    id: platformIds.disabled,
    username: "tansy",
    display_name: "Tansy Roe",
    active: false,
    permissions: [],
  }),
];

export const platformInvitation = (
  overrides: Partial<PlatformInvitation> = {},
): PlatformInvitation => {
  const id = overrides.id ?? platformIds.unshown;
  return {
    id,
    recipient_label: "Juniper",
    status: "awaiting_delivery",
    expires_at: "2026-11-18T12:00:00+00:00",
    delivery_reference: `/grimoire/platform?invitation_id=${id}`,
    ...overrides,
  };
};

export const platformInvitations: PlatformInvitation[] = [
  platformInvitation(),
  platformInvitation({
    id: platformIds.shown,
    recipient_label: "Corin",
    status: "pending",
  }),
  platformInvitation({
    id: platformIds.accepted,
    recipient_label: "Tansy",
    status: "accepted",
  }),
];

const delivered = (id: string): DeliveredInvitation => ({
  ...platformInvitation({ id, status: "pending" }),
  audit_reference: "c1111111-1111-4111-8111-111111111111",
  token: platformToken,
});

/** Keyed `METHOD /full/path`: these endpoints sit outside the Grimoire API. */
export const platformOther: OtherRoutes = {
  "GET /api/auth/platform/management/users": {
    items: platformUsers,
    next: null,
  },
  "GET /api/auth/platform/management/invitations": {
    items: platformInvitations,
    next: null,
  },
  "GET /api/auth/platform/management/permissions": platformPermissions,
  "GET /api/auth/platform/self": platformUser(),
  "POST /api/auth/platform/commands": {
    ...platformUser(),
    audit_reference: "c2222222-2222-4222-8222-222222222222",
  },
  "POST /api/auth/platform/invitations/{invitation_id}/deliver": (call: Call) =>
    delivered(call.params.invitation_id!),
};
