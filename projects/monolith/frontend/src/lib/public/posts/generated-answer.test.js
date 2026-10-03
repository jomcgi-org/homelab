import { expect, test } from "vitest";
import {
  splitGeneratedAnswer,
  streamedGraphParts,
} from "./generated-answer.js";

test("streams prose before a fence and withholds incomplete Mermaid statements", () => {
  expect(splitGeneratedAnswer("A simple explanation.").prose).toBe(
    "A simple explanation.",
  );
  const partial = splitGeneratedAnswer(
    "A simple explanation.\n\n```mermaid\nflowchart LR\nA[Router] --> B[GPU]\nB --> C[",
  );
  expect(partial.prose).toBe("A simple explanation.");
  expect(partial.code).toContain("B --> C[");
  expect(partial.renderable).toBe("flowchart LR\nA[Router] --> B[GPU]");
});

test("a closed fence includes its last statement without inventing source", () => {
  const result = splitGeneratedAnswer(
    "Explanation.\n```mermaid\nflowchart LR\nA --> B```",
  );
  expect(result.renderable).toBe("flowchart LR\nA --> B");
  expect(result.code).not.toContain("```");
});

test("diagram visibility follows completed statements without revealing future nodes", () => {
  const partial = splitGeneratedAnswer(
    "```mermaid\nflowchart LR\nA[Request] --> B[Router]\nB --> C[GPU\n",
  );
  const parts = streamedGraphParts(partial.renderable);
  expect([...parts.nodes]).toEqual(["A", "B"]);
  expect([...parts.edges]).toEqual(["A->B"]);
});
