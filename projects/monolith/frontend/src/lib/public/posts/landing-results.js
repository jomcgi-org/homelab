// Headline numbers shown before the replay hydrates. The full recordings are
// code-split (the coding one is ~170 kB), so the landing page server-renders
// these two rows instead; landing-results.test.js keeps them equal to the
// recordings' own metrics.
export const landingResults = [
  {
    kind: "research",
    label: "Read an incident report",
    inputTokens: 21877,
    firstTokenSeconds: 6.98,
    decodeRate: 46.0,
  },
  {
    kind: "coding",
    label: "Rewrite a source file",
    inputTokens: 23725,
    firstTokenSeconds: 7.24,
    decodeRate: 43.9,
  },
];
