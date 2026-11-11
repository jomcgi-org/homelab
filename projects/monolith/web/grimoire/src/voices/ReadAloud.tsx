import type { SessionEvent } from "../api/session";
import type { VoicePreset } from "./voices";

export interface ReadAloudProps {
  campaignId: string;
  role: "dm" | "player";
  /** The viewer's character, when they have one. */
  characterId: string | null;
  events: SessionEvent[];
  voices: VoicePreset[];
}

/** Opt-in speech for new narration, using the browser's speech synthesis. */
export function ReadAloud(_props: ReadAloudProps) {
  return null;
}
