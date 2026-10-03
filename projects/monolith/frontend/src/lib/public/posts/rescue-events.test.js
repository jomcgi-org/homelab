import { expect, test } from "vitest";
import { rescueEvents } from "./rescue-events.js";
const failure = JSON.stringify({
  id: "failure",
  title: "Oxygen tank fails",
  detail: "The service module loses oxygen and power.",
});
const lifeboat = JSON.stringify({
  id: "lifeboat",
  title: "Use the lunar module",
  detail: "The crew switches life support.",
});
test("reveals only complete ordered events, including the final line on completion", () => {
  expect(rescueEvents(failure)).toEqual([]);
  expect(rescueEvents(failure + "\n" + lifeboat.slice(0, 30))).length(1);
  expect(rescueEvents(failure + "\n" + lifeboat, true)).length(2);
  expect(rescueEvents(lifeboat + "\n")).toEqual([]);
  expect(rescueEvents('{"id":"failure","title":42,"detail":"bad"}\n')).toEqual(
    [],
  );
});
