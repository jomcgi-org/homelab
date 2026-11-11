// Synthetic public data for DOM and browser evidence. Coordinates exercise
// the production projection without contacting a backend or private corpus.
export const graphFixture = {
  nodes: [
    {
      id: "sheet-note",
      title: "Public graph rendering with a deliberately long note title",
      type: "atom",
      degree: 6,
      x: 0,
      y: 0,
    },
    {
      id: "tokens",
      title: "Scheme tokens",
      type: "fact",
      degree: 2,
      x: -0.6,
      y: -0.5,
    },
    {
      id: "source",
      title: "Source observations",
      type: "raw",
      degree: 1,
      x: 0.2,
      y: -0.7,
    },
    {
      id: "research",
      title: "Research questions",
      type: "gap",
      degree: 1,
      x: 0.7,
      y: -0.2,
    },
    {
      id: "work",
      title: "Active work",
      type: "active",
      degree: 1,
      x: 0.6,
      y: 0.6,
    },
    {
      id: "reference",
      title: "Reference paper",
      type: "paper",
      degree: 1,
      x: -0.2,
      y: 0.7,
    },
    {
      id: "other",
      title: "Unclassified note",
      type: "other",
      degree: 1,
      x: -0.7,
      y: 0.2,
    },
  ],
  edges: ["tokens", "source", "research", "work", "reference", "other"].map(
    (id) => ({ source: "sheet-note", target: id, kind: "related" }),
  ),
};

export const noteFixture = {
  body: [
    "## A public note",
    "Synthetic browser evidence, not a production note. [[Scheme tokens]] connects this note to another node.",
    "- Keep the graph positions stable.",
    "- Change the scheme on the mounted canvas.",
    "### Long content",
    "`" + "long_identifier_".repeat(18) + "`",
    "```\n" + "wide code column ".repeat(16) + "\n```",
    "| Property | Observation |\n| --- | --- |\n| Palette | Public sheet tokens |\n| Corpus | Synthetic public data |",
    ...Array.from(
      { length: 4 },
      () =>
        "Paragraphs keep the panel scrollable while the close control stays visible.",
    ),
  ].join("\n\n"),
};
