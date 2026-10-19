// Invented records for the normally private review controls. No server loader.
export const records = {
  gaps: {
    id: "fixture-gap",
    term: "Synthetic garden sensor",
    context: "An invented maintenance question",
    gap_class: "internal",
    state: "resolved",
    human_verified: false,
    referenced_by_count: 2,
    research_attempts: 1,
    answer: "The fictional sensor is checked every Monday.",
    stub_body: "Invented review content. No private notes or cluster records.",
  },
  notes: {
    id: "fixture-note",
    title: "Synthetic workshop checklist",
    snippet: "Check the fictional workbench and record an invented reading.",
    visibility: "private",
    visibility_verified: true,
    type: "note",
    source: "invented fixture",
    tags: ["synthetic", "workshop"],
  },
};
