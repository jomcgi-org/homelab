/**
 * Invented data shaped by the generated API schema. No real campaign, player
 * or identifier appears here. Tests and the fixture preview share it.
 */
import type {
  CharacterView,
  InvitationView,
  LobbyCampaignView,
  LobbyView,
  MemberView,
} from "../api/schema";

export const ids = {
  owned: "11111111-1111-4111-8111-111111111111",
  joined: "22222222-2222-4222-8222-222222222222",
  seatless: "33333333-3333-4333-8333-333333333333",
  dmMember: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  playerMember: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  mira: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  spare: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  invitation: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

const campaign = (
  overrides: Partial<LobbyCampaignView> &
    Pick<LobbyCampaignView, "id" | "name">,
): LobbyCampaignView => ({
  dm_name: "Ash",
  created_at: "2026-10-01T18:00:00Z",
  role: "player",
  is_owner: false,
  player_character_id: null,
  character_name: null,
  ...overrides,
});

export const campaigns = {
  owned: campaign({
    id: ids.owned,
    name: "The Sunless Citadel",
    role: "dm",
    is_owner: true,
  }),
  joined: campaign({
    id: ids.joined,
    name: "Salt Marsh Nights",
    player_character_id: ids.mira,
    character_name: "Mira Vale",
  }),
  seatless: campaign({ id: ids.seatless, name: "The Long Road" }),
};

export const invitation: InvitationView = {
  id: ids.invitation,
  campaign_id: "44444444-4444-4444-8444-444444444444",
  campaign_name: "Embers of the Old Court",
  invitee_email: "ash@example.test",
  status: "pending",
};

export const lobby = (overrides: Partial<LobbyView> = {}): LobbyView => ({
  can_administer_accounts: false,
  new_feedback_count: 0,
  invitation_enrollment_enabled: false,
  can_create_game: true,
  user: {
    id: "99999999-9999-4999-8999-999999999999",
    email: "ash@example.test",
    display_name: "Ash",
  },
  campaigns: [campaigns.owned, campaigns.joined, campaigns.seatless],
  invitations: [invitation],
  ...overrides,
});

export const members: MemberView[] = [
  {
    id: ids.dmMember,
    email: "ash@example.test",
    role: "dm",
    player_character_id: null,
    character_name: null,
    created_at: "2026-10-01T18:00:00Z",
  },
  {
    id: ids.playerMember,
    email: "rowan@example.test",
    role: "player",
    player_character_id: null,
    character_name: null,
    created_at: "2026-10-02T18:00:00Z",
  },
];

export const characters: CharacterView[] = [
  {
    id: ids.spare,
    campaign_id: ids.owned,
    player_name: null,
    character_name: "Brannoch Stone",
    class_name: "Fighter",
    level: 3,
    sheet: {},
  },
];
