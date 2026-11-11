import type { VoicePreset } from "./voices";

export interface VoicePresetsProps {
  campaignId: string;
  presets: VoicePreset[];
}

/** The DM's saved read-aloud voices for the narrator and NPCs. */
export function VoicePresets(_props: VoicePresetsProps) {
  return null;
}
