// Interactive figures a post can name with a ```interactive marker. Loaded on
// demand so a post pays only for the figures it uses.
export const figures = {
  fit: () => import("./Fit.svelte"),
  routing: () => import("./Routing.svelte"),
  residency: () => import("./Residency.svelte"),
  context: () => import("./Context.svelte"),
  "copy-queue": () => import("./CopyQueue.svelte"),
  "draft-pays": () => import("./DraftPays.svelte"),
  precision: () => import("./Precision.svelte"),
  "prefix-race": () => import("./PrefixRace.svelte"),
  concurrency: () => import("./Concurrency.svelte"),
  published: () => import("./Published.svelte"),
};
