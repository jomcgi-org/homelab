// Maps project ids from engineering-data.js to diagram components.
// Task 4 adds the apps + build entries.
import AgentPlatform from "./AgentPlatform.svelte";
import KnowledgeGraph from "./KnowledgeGraph.svelte";
import Loom from "./Loom.svelte";
import OciModelCache from "./OciModelCache.svelte";
import Sextant from "./Sextant.svelte";
import CloudflareOperator from "./CloudflareOperator.svelte";

export const diagrams = {
  "agent-platform": AgentPlatform,
  "knowledge-graph": KnowledgeGraph,
  loom: Loom,
  "oci-model-cache": OciModelCache,
  sextant: Sextant,
  "cloudflare-operator": CloudflareOperator,
};
