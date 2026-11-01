import { expect, test } from "vitest";
import {
  ONE_TOKEN_RATE,
  breakEven,
  draftRate,
  verdict,
} from "./data-draft-pays.js";

test("an 8-token step yields kept + 1 tokens over 110 to 125 ms", () => {
  expect(ONE_TOKEN_RATE).toBe(40);
  const r = draftRate(5);
  expect(r.low).toBeCloseTo(48);
  expect(r.high).toBeCloseTo(54.55, 1);
  expect(draftRate(7).high).toBeCloseTo(72.7, 1);
});

test("a draft loses below 4 kept, ties at 4 and pays above", () => {
  expect(verdict(3)).toBe("loses");
  expect(verdict(4)).toBe("even");
  expect(verdict(5)).toBe("pays");
  expect(breakEven.low).toBeCloseTo(3.4);
  expect(breakEven.high).toBeCloseTo(4);
});
