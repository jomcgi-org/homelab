// Figures a post can name with a ```figure marker. Static and server-rendered:
// the page needs no JavaScript to show them.
import Concurrency from "./Concurrency.svelte";
import Context from "./Context.svelte";
import CopyQueue from "./CopyQueue.svelte";
import DraftPays from "./DraftPays.svelte";
import Fit from "./Fit.svelte";
import Precision from "./Precision.svelte";
import PrefixRace from "./PrefixRace.svelte";
import Published from "./Published.svelte";
import Residency from "./Residency.svelte";

export const figures = {
  fit: Fit,
  residency: Residency,
  context: Context,
  "copy-queue": CopyQueue,
  "draft-pays": DraftPays,
  precision: Precision,
  "prefix-race": PrefixRace,
  concurrency: Concurrency,
  published: Published,
};
