import { expect, test } from "vitest";
import { parseArguments } from "./src/parseArguments";

test("preserves quoted selectors and joined argument fragments", () => {
  expect(
    parseArguments("kubectl get pods -n team-a -l 'app in (api,worker)'"),
  ).toEqual([
    "kubectl",
    "get",
    "pods",
    "-n",
    "team-a",
    "-l",
    "app in (api,worker)",
  ]);
  expect(parseArguments('a\\ b "" --"server"=host "say \\"hi\\""')).toEqual([
    "a b",
    "",
    "--server=host",
    'say "hi"',
  ]);
});

test("rejects incomplete quotes and escapes", () => {
  for (const line of ["get 'pods", 'get "pods', "get pods\\"])
    expect(() => parseArguments(line)).toThrow();
});
