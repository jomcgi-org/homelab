// Maps project ids from engineering-data.js to diagram components.
// Keys must stay in sync with registry-ids.js (checked below).
import AgentPlatform from "./AgentPlatform.svelte";
import EmberVm from "./EmberVm.svelte";
import KnowledgeGraph from "./KnowledgeGraph.svelte";
import Loom from "./Loom.svelte";
import OciModelCache from "./OciModelCache.svelte";
import Sextant from "./Sextant.svelte";
import CloudflareOperator from "./CloudflareOperator.svelte";
import Trips from "./Trips.svelte";
import Ships from "./Ships.svelte";
import Stargazer from "./Stargazer.svelte";
import Bazel from "./Bazel.svelte";
import RulesSemgrep from "./RulesSemgrep.svelte";
import { diagramIds } from "./registry-ids.js";

export const diagrams = {
  "agent-platform": AgentPlatform,
  embervm: EmberVm,
  "knowledge-graph": KnowledgeGraph,
  loom: Loom,
  "oci-model-cache": OciModelCache,
  sextant: Sextant,
  "cloudflare-operator": CloudflareOperator,
  trips: Trips,
  ships: Ships,
  stargazer: Stargazer,
  bazel: Bazel,
  "rules-semgrep": RulesSemgrep,
};

// Drift check: registry-ids.js is the plain-JS mirror used by tests
// (vitest's node env cannot parse .svelte imports). Fail loudly if the
// two files ever disagree.
for (const id of diagramIds) {
  if (!diagrams[id]) throw new Error(`diagrams/index.js missing ${id}`);
}
