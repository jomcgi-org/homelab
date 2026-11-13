import { Collapsible } from "@astryxdesign/core/Collapsible";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { loadEventAudio } from "../api/tts";
import type { SessionEvent } from "../api/session";
import {
  createEventReader,
  createSpeaker,
  loadReadAloudSettings,
  saveReadAloudSettings,
} from "../lib/read-aloud.js";
import type { VoicePreset } from "./voices";

export interface ReadAloudProps {
  campaignId: string;
  role: "dm" | "player";
  /** The viewer's character, when they have one. */
  characterId: string | null;
  events: SessionEvent[];
  voices: VoicePreset[];
}

interface Settings {
  narration: boolean;
  reveals: boolean;
}

/** The speech queue, as the untyped read-aloud module builds it. */
interface Speaker {
  available: boolean;
  stop: () => void;
  reconcile: (events: SessionEvent[]) => void;
  deliver: (items: unknown[], presets: VoicePreset[]) => void;
  dispose: () => void;
}
const newSpeaker = createSpeaker as unknown as (options: {
  onSpeakingChange: (speaking: boolean) => void;
  serverAudio:
    ((event: SessionEvent, signal: AbortSignal) => Promise<Blob>) | null;
}) => Speaker;

/** The choices are kept on this device. Reading it throws where storage is disabled. */
function deviceStorage(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

/** Opt-in speech for new narration, using the browser's speech synthesis. */
export function ReadAloud({
  campaignId,
  role,
  characterId,
  events,
  voices,
}: ReadAloudProps) {
  const [settings, setSettings] = useState(
    () => loadReadAloudSettings(deviceStorage(), campaignId, role) as Settings,
  );
  const reader = useMemo(
    () => createEventReader(),
    [campaignId, role, characterId],
  );
  const [speaker, setSpeaker] = useState<Speaker | null>(null);
  const [speaking, setSpeaking] = useState(false);

  useEffect(() => {
    const created = newSpeaker({
      onSpeakingChange: setSpeaking,
      serverAudio:
        role === "dm"
          ? (event, signal) => loadEventAudio(campaignId, event.id, signal)
          : null,
    });
    setSettings(
      loadReadAloudSettings(deviceStorage(), campaignId, role) as Settings,
    );
    setSpeaker(created);
    return () => created.dispose();
  }, [campaignId, role, characterId]);

  // The reader's first pass marks the history already on screen as handled,
  // so only events that arrive afterwards are spoken.
  useEffect(() => {
    if (!speaker) return;
    speaker.reconcile(events);
    speaker.deliver(
      reader.select(events, { role, pcId: characterId, ...settings }),
      voices,
    );
  }, [speaker, reader, events, role, characterId, settings, voices]);

  if (!speaker?.available) return null;

  const choose = (change: Partial<Settings>) => {
    const next = { ...settings, ...change };
    setSettings(next);
    speaker.stop();
    saveReadAloudSettings(deviceStorage(), campaignId, next);
  };

  // A set-once preference, so it stays folded away above the story.
  return (
    <Collapsible
      defaultIsOpen={false}
      trigger={<Text type="label">Read aloud</Text>}
    >
      <VStack gap={2} as="section" aria-label="Read aloud">
        <HStack gap={4} wrap="wrap" vAlign="center">
          <CheckboxInput
            label="Read DM narration"
            value={settings.narration}
            onChange={(narration) => choose({ narration })}
          />
          {role !== "dm" && (
            <CheckboxInput
              label="Read my reveals and private narration"
              value={settings.reveals}
              onChange={(reveals) => choose({ reveals })}
            />
          )}
          <Button
            label="Stop read-aloud"
            isDisabled={!speaking}
            onClick={() => speaker.stop()}
          />
        </HStack>
        <Text type="supporting" as="p">
          Saved on this device for this campaign. For a table speaker, the DM
          turns narration off and one table member turns it on. Private messages
          and reveals never play on the DM room speaker.
        </Text>
      </VStack>
    </Collapsible>
  );
}
