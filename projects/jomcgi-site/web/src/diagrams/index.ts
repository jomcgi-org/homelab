import type { ComponentType } from "react";
import AgentPlatform from "./AgentPlatform";
import EmberVm from "./EmberVm";
import KnowledgeGraph from "./KnowledgeGraph";
import Loom from "./Loom";
import OciModelCache from "./OciModelCache";
import Sextant from "./Sextant";
import CloudflareOperator from "./CloudflareOperator";
import Trips from "./Trips";
import Ships from "./Ships";
import Stargazer from "./Stargazer";
import Bazel from "./Bazel";

export const diagrams: Record<string, ComponentType> = {
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
};
