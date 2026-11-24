import { expect, it } from "vitest";
import type { IssueSummary } from "../../../../factory-storyboard/schema/types";
import { activeFirst } from "./Pages";

const issue = (issueKey: string, state: IssueSummary["state"]) =>
  ({ issueKey, state }) as IssueSummary;

it("puts moving work first and keeps each group in its own order", () => {
  const ordered = activeFirst([
    issue("HOME-1", "In review"),
    issue("HOME-2", "Working"),
    issue("HOME-3", "In review"),
    issue("HOME-4", "In merge queue"),
    issue("HOME-5", "Working"),
  ]);
  expect(ordered.map(({ issueKey }) => issueKey)).toEqual([
    "HOME-2",
    "HOME-5",
    "HOME-4",
    "HOME-1",
    "HOME-3",
  ]);
});

it("does not change the list it is given", () => {
  const input = [issue("HOME-1", "In review"), issue("HOME-2", "Working")];
  activeFirst(input);
  expect(input.map(({ issueKey }) => issueKey)).toEqual(["HOME-1", "HOME-2"]);
});
