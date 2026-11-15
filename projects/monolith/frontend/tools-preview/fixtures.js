// Invented records only. These fixtures never call private services.
export const longText =
  "Synthetic fixture: " + "long-reference-without-a-break".repeat(12);
export const reviewData = {
  tab: "gaps",
  mode: "pending",
  error: null,
  items: [
    {
      id: "fixture-gap",
      term: longText,
      context: "Synthetic context",
      gap_class: "internal",
      stub_body: (
        "A synthetic paragraph with no private data.\n" +
        longText +
        "\n"
      ).repeat(12),
      referenced_by_count: 2,
      research_attempts: 1,
    },
    {
      id: "fixture-gap-2",
      term: "Second synthetic gap",
      gap_class: "internal",
      context: "Second context",
    },
  ],
};
export const notesData = {
  graph: {
    nodes: [
      {
        id: "fixture-note",
        title: "Synthetic note",
        type: "fact",
        degree: 0,
        x: 0,
        y: 0,
      },
    ],
    edges: [],
    indexed_at: "2026-11-01T12:00:00Z",
  },
};
export const noteDetail = {
  id: "fixture-note",
  title: "Synthetic note",
  type: "fact",
  content:
    "# Synthetic note\n\n" +
    longText +
    "\n\n" +
    "Fixture paragraph.\n\n".repeat(20),
  edges: [],
  tags: ["synthetic"],
  review_history: { entries: [], has_more: false },
};
export const stats = {
  stats: {
    model: { id: "synthetic-model" },
    kv: { used_pages: 12, total_pages: 100 },
    vram_bytes: 1024,
    gpus: [{ total_bytes: 4096 }],
    throughput: { decode_tps: 35, prefill_tps: 100 },
    requests: { active: 0, completed: 3 },
    uptime_s: 600,
  },
  recent: [
    {
      ts: "2026-11-01T12:00:00Z",
      prompt_tokens: 25,
      completion_tokens: 12,
      ttft_ms: 50,
      duration_ms: 1000,
    },
  ],
};
export const qwenSession = {
  id: "fixture-session",
  title: longText,
  createdAt: 1793534400000,
  messages: [
    { role: "user", content: longText },
    {
      role: "assistant",
      content:
        "## Synthetic answer\n\n" +
        longText +
        "\n\n```text\n" +
        longText +
        "\n```",
      turn: {
        sentAt: 0,
        firstTokenAt: 50,
        answerAt: 80,
        endedAt: 1000,
        reasoning: "Synthetic reasoning",
        content: "Synthetic answer",
        usage: { prompt_tokens: 25, completion_tokens: 12 },
      },
    },
  ],
};
const ability_scores = Object.fromEntries(
  [
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
  ].map((name) => [name, 12]),
);
export function grimoireData(role = "player", status = "draft") {
  return {
    unavailable: false,
    groups: [
      {
        campaign: { id: "fixture-campaign", name: "Synthetic campaign" },
        workspaces: [
          {
            viewer_role: role,
            character: {
              id: "fixture-character",
              character_name: longText,
              class_name: "Wizard",
              level: 3,
            },
            versions: [
              {
                id: "fixture-version",
                version: 1,
                status,
                created_at: "2026-11-01T12:00:00Z",
                sheet: {
                  ancestry: "Human",
                  class_name: "Wizard",
                  level: 3,
                  ability_scores,
                },
                derived: {
                  proficiency_bonus: 2,
                  unarmored_armor_class: 11,
                  initiative: 1,
                  ability_modifiers: ability_scores,
                  saving_throw_bonuses: ability_scores,
                },
              },
            ],
          },
        ],
      },
    ],
  };
}
export const chatEvents = [
  {
    type: "text_chunk",
    data: { text: "Synthetic streamed answer. " + longText },
  },
  {
    type: "node_discovered",
    data: { note_id: "fixture-note", title: "Synthetic note", type: "fact" },
  },
  { type: "done", data: {} },
];
export const qwenEvents = [
  { choices: [{ delta: { reasoning_content: "Synthetic reasoning" } }] },
  {
    choices: [
      { delta: { content: "Synthetic streamed Qwen answer. " + longText } },
    ],
  },
  {
    choices: [{ delta: {}, finish_reason: "stop" }],
    usage: {
      prompt_tokens: 25,
      completion_tokens: 12,
      prompt_tokens_details: { cached_tokens: 5 },
    },
  },
];
export function sse(events) {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}
