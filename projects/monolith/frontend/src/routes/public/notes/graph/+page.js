// The page is a canvas-driven force graph, so SSR adds latency without UX value
// and avoids Svelte 5 hydration issues with d3-mounted components. This is the
// standalone full-screen graph; the /notes landing embeds the same graph as a
// chat deep-dive overlay (ADR 005).
export const ssr = false;
