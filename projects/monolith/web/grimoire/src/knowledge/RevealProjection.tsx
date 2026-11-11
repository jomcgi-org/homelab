/** One revealed entity as a reveal event carries it. */
export interface RevealedKnowledge {
  entity_id: string;
  name: string;
  grant_scope?: "name_only" | "partial" | "full" | string;
  retracted?: boolean;
  silent?: boolean;
  [key: string]: unknown;
}

export interface RevealProjectionProps {
  knowledge: RevealedKnowledge;
}

/** What a player was shown: name, type and any revealed details. */
export function RevealProjection(_props: RevealProjectionProps) {
  return null;
}
