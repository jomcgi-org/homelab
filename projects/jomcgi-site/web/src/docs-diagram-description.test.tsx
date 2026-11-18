import mermaid from "mermaid";
import { beforeAll, expect, it } from "vitest";
import { describeDiagram, plainText } from "./docs-diagram-description";

beforeAll(() =>
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    htmlLabels: false,
  }),
);

async function describe(source: string) {
  return describeDiagram(await mermaid.mermaidAPI.getDiagramFromText(source));
}

it("describes labels, groups, decisions and each kind of connection", async () => {
  const { statements } = await describe(`flowchart LR
    subgraph host[Trusted host]
      A[Worker] <--> B[Store]
    end
    B -. feedback .-> C{Ready?}
    A --- C
    C -->|yes| D[Serve]
    Z[Isolated node]
  `);
  expect(statements).toContain(
    "Nodes: Worker; Store; Ready?; Serve; Isolated node.",
  );
  expect(statements).toContain("Group Trusted host contains: Store; Worker.");
  expect(statements).toContain("Decision: Ready?.");
  expect(statements).toContain("Worker and Store connect in both directions.");
  expect(statements).toContain("Store points to Ready? (dashed): feedback.");
  expect(statements).toContain("Worker connects with Ready? (no direction).");
  expect(statements).toContain("Ready? points to Serve: yes.");
});

it("keeps sequence participants, message order, replies and self-calls", async () => {
  const { statements } = await describe(`sequenceDiagram
    participant C as Caller
    participant S as Server
    C->>S: request
    S->>S: check quota
    S-->>C: result
  `);
  expect(statements).toEqual([
    "Participants: Caller; Server. Messages in order:",
    "1. Caller sends to Server: request.",
    "2. Server sends to Server: check quota.",
    "3. Server sends to Caller (dashed arrow): result.",
  ]);
});

it("describes initial and final states, branching and guarded retries", async () => {
  const { statements } = await describe(`stateDiagram-v2
    [*] --> Pending
    Pending --> Ready: CacheHit
    Pending --> Failed: MarkFailed
    Failed --> Pending: Retry [retryable]
    Ready --> [*]
  `);
  expect(statements).toEqual([
    "States: Start; Pending; Ready; Failed; End.",
    "Start transitions to Pending.",
    "Pending transitions to Ready: CacheHit.",
    "Pending transitions to Failed: MarkFailed.",
    "Failed transitions to Pending: Retry [retryable].",
    "Ready transitions to End.",
  ]);
});

it("converts HTML labels to readable text without joining words or exposing markup", () => {
  expect(plainText("<b>Worker</b><br/>reads &amp; writes\n data")).toBe(
    "Worker reads & writes data",
  );
});

it("requires an authored description for unsupported semantics instead of dropping them", async () => {
  const source = `sequenceDiagram
    participant A as Client
    participant B as Service
    alt cache miss
      A->>B: fetch
    else cache hit
      B-->>A: cached result
    end
  `;
  await expect(describe(source)).rejects.toThrow("Add Mermaid accDescr");
  const description =
    "Client fetches from Service on a cache miss. Service returns the cached result on a cache hit.";
  const authored = await describe(
    source.replace(
      "sequenceDiagram",
      `sequenceDiagram\naccDescr: ${description}`,
    ),
  );
  expect(authored.statements).toEqual([description]);
  await expect(describe('pie\n"one": 1')).rejects.toThrow(
    "Add Mermaid accDescr",
  );
});
