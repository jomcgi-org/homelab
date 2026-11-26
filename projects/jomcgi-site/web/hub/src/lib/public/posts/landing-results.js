// Headline numbers shown before the replay hydrates. The full recordings are
// code-split (the coding one is ~170 kB), so the landing page server-renders
// these two rows instead; landing-results.test.js keeps them equal to the
// recordings' own metrics.
export const landingResults = [
  {
    kind: "research",
    label: "Read an incident report",
    inputTokens: 21878,
    firstTokenSeconds: 6.95,
    decodeRate: 45.4,
  },
  {
    kind: "coding",
    label: "Rewrite a source file",
    inputTokens: 23723,
    firstTokenSeconds: 7.33,
    decodeRate: 46.3,
  },
];
