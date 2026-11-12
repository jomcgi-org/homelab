/**
 * Every in-app destination. Paths are relative to the router's basename, so
 * moving the app (GRIMOIRE_BASE) changes nothing here.
 */
export const paths = {
  lobby: "/",
  sheets: (characterId?: string | null) =>
    characterId ? `/sheets#character-${characterId}` : "/sheets",
  session: (campaignId: string) => `/campaigns/${campaignId}/session`,
  notes: (campaignId: string) => `/campaigns/${campaignId}/notes`,
  journal: (campaignId: string) => `/campaigns/${campaignId}/journal`,
  grants: (campaignId: string) => `/campaigns/${campaignId}/grants`,
  joinAccept: "/join/accept",
  platform: "/platform",
  profile: "/platform/profile",
  feedback: "/feedback",
  entity: (campaignId: string, entityId: string) =>
    `/campaigns/${campaignId}/entities/${entityId}`,
};

/** The gateway serves these itself; they are full page navigations. */
export const gateway = {
  signOut: "/grimoire/oauth2/logout",
  /** The anonymous landing a single-use link opens; served by the backend. */
  joinLanding: "/grimoire/join",
};

/** The router basename: Vite's base without its trailing slash. */
export const basename = import.meta.env.BASE_URL.replace(/\/$/, "");
