// The notes app is a client-interactive chat box, and the graph view it can
// switch to is a canvas-driven force graph. SSR adds latency without UX value
// and avoids Svelte 5 hydration issues with d3-mounted components.
export const ssr = false;
