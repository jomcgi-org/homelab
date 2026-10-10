import { knowledgeFields, projectionOf, scopeOf } from "./knowledge-fields.js";

export function resolveVoice(voices, hint = {}) {
  for (const name of hint?.names || []) {
    const match = voices.find((voice) =>
      voice.name.toLowerCase().includes(name.toLowerCase()),
    );
    if (match) return match;
  }
  const lang = hint?.lang?.toLowerCase();
  return (
    (lang && voices.find((voice) => voice.lang.toLowerCase() === lang)) ||
    (lang && voices.find((voice) => voice.lang.toLowerCase().split("-")[0] === lang.split("-")[0])) ||
    voices.find((voice) => voice.default)
  );
}

export function voicePreset(presets, key) {
  return presets.find((row) => row.speaker_key === key) ||
    presets.find((row) => row.speaker_key === "narrator") ||
    { rate: 1, pitch: 1, voice_hint: {} };
}

export function readAloudDefaults(role) {
  return { narration: role === "dm", reveals: false };
}

const storageKey = (campaignId) => `grimoire:read-aloud:${campaignId}`;
export function loadReadAloudSettings(storage, campaignId, role) {
  const defaults = readAloudDefaults(role);
  try {
    const saved = JSON.parse(storage?.getItem(storageKey(campaignId)) || "null");
    return Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
      [key, typeof saved?.[key] === "boolean" ? saved[key] : value],
    ));
  } catch { return defaults; }
}

export function saveReadAloudSettings(storage, campaignId, settings) {
  try { storage?.setItem(storageKey(campaignId), JSON.stringify(settings)); } catch { /* Private browsing can disable storage. */ }
}

function revealText(item) {
  const projection = projectionOf(item);
  const details = scopeOf(item) === "name_only" ? [] : knowledgeFields(projection);
  return [projection.name, ...details.map(([key, value]) =>
    `${key.replaceAll("_", " ")}: ${typeof value === "string" ? value : JSON.stringify(value)}`,
  )].filter(Boolean).join(". ");
}

// Receives only the already-authorized page feed. This is selection, never a fetch.
export function createEventReader() {
  const handled = new Set();
  let initialized = false;
  return {
    select(events, { role, pcId, narration, reveals }) {
      const fresh = events.filter((event) => !handled.has(event.id));
      for (const event of events) handled.add(event.id);
      if (!initialized) { initialized = true; return []; }
      return fresh.sort((a, b) => a.seq - b.seq).flatMap((event) => {
        if (event.retracted_at || !event.body) return [];
        if (event.kind === "narration") {
          const table = event.audience === "table" && narration;
          const privateReply = role !== "dm" && reveals && pcId &&
            event.audience === "pcs" && event.audience_pc_ids?.includes(pcId);
          return (table || privateReply) && event.body.text ? [{ event, text: event.body.text }] : [];
        }
        if (role === "dm" || !reveals || event.kind !== "reveal") return [];
        return (event.body.reveals || [event.body])
          .filter((item) => item && !item.silent && !item.retracted &&
            !event.body.retracted_entity_ids?.includes(item.entity_id))
          .map((item) => ({ event, text: revealText(item) }))
          .filter((item) => item.text);
      });
    },
  };
}

export function createSpeaker({
  synth = globalThis.speechSynthesis,
  Utterance = globalThis.SpeechSynthesisUtterance,
  micPauser = { pause() {}, resume() {} },
  onSpeakingChange = () => {},
} = {}) {
  const available = Boolean(synth && Utterance);
  let voices = available ? synth.getVoices() : [];
  let queue = [];
  let current = null;
  let generation = 0;
  let running = false;
  const updateVoices = () => { voices = synth.getVoices(); };
  if (available) synth.addEventListener?.("voiceschanged", updateVoices);

  function finishRun() {
    if (!running) return;
    running = false;
    micPauser.resume();
    onSpeakingChange(false);
  }
  function stop() {
    generation += 1;
    queue = [];
    current = null;
    if (available) synth.cancel();
    finishRun();
  }
  function advance() {
    if (current || !available) return;
    const next = queue.shift();
    if (!next) { finishRun(); return; }
    if (!running) {
      running = true;
      micPauser.pause();
      onSpeakingChange(true);
    }
    const utterance = new Utterance(next.text);
    const preset = next.preset;
    const voice = resolveVoice(voices, preset.voice_hint);
    if (voice) utterance.voice = voice;
    if (preset.voice_hint?.lang) utterance.lang = preset.voice_hint.lang;
    utterance.rate = preset.rate ?? 1;
    utterance.pitch = preset.pitch ?? 1;
    current = utterance;
    const run = generation;
    const done = () => {
      if (run !== generation || current !== utterance) return;
      current = null;
      advance();
    };
    utterance.onend = done;
    utterance.onerror = done;
    try { synth.speak(utterance); } catch { done(); }
  }
  // Cancel older deliveries once, then enqueue this delivery in sequence order.
  function deliver(items, presets = []) {
    if (!available || !items.length) return;
    if (items.some(({ event }) => event.kind === "narration")) stop();
    queue.push(...[...items].sort((a, b) => a.event.seq - b.event.seq).map(({ event, text }) => ({
      text, preset: voicePreset(presets, event.body?.speaker_key),
    })));
    advance();
  }
  return {
    available, stop, deliver,
    speak(event, presets = []) { deliver([{ event, text: event.body?.text || "" }], presets); },
    dispose() { stop(); if (available) synth.removeEventListener?.("voiceschanged", updateVoices); },
  };
}
