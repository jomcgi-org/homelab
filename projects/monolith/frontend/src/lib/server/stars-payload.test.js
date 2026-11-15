import { describe, expect, it } from "vitest";
import { shareForecastHours } from "./stars-payload.js";

const hour = {
  time: "2026-11-14T22:00:00+00:00",
  cloud_area_fraction: 4.5,
  dark: true,
};

function snapshot() {
  return {
    count: 2,
    fetched_at: "2026-11-14T21:00:00+00:00",
    sites: [
      { id: "first", best_hours: [{ ...hour }, { ...hour, dark: false }] },
      { id: "second", best_hours: [{ ...hour }, { ...hour, dark: false }] },
    ],
  };
}

function freeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

describe("Stars forecast serialization", () => {
  it("shares identical complete hours without changing any JSON value", () => {
    const input = snapshot();
    const result = shareForecastHours(input);
    expect(result).toEqual(input);
    expect(JSON.stringify(result)).toBe(JSON.stringify(input));
    expect(result.sites[0].best_hours[0]).toBe(result.sites[1].best_hours[0]);
    expect(result.sites[0].best_hours[1]).toBe(result.sites[1].best_hours[1]);
    expect(result.sites[0].best_hours).not.toBe(result.sites[1].best_hours);
  });

  it.each([
    { time: "2026-11-14T23:00:00+00:00" },
    { cloud_area_fraction: 4.6 },
    { dark: false },
    { extra: null },
    { extra: { model: "next", confidence: [0, 1] } },
  ])("keeps a different complete JSON record separate: %o", (difference) => {
    const input = snapshot();
    input.sites[1].best_hours[0] = { ...hour, ...difference };
    const result = shareForecastHours(input);
    expect(result.sites[0].best_hours[0]).not.toBe(
      result.sites[1].best_hours[0],
    );
    expect(JSON.stringify(result)).toBe(JSON.stringify(input));
  });

  it("does not mutate the parsed response, its site rows or hour arrays", () => {
    const input = freeze(snapshot());
    const result = shareForecastHours(input);
    expect(result).toEqual(input);
    expect(result).not.toBe(input);
    expect(result.sites).not.toBe(input.sites);
    expect(result.sites[0]).not.toBe(input.sites[0]);
    expect(result.sites[0].best_hours).not.toBe(input.sites[0].best_hours);
    expect(input.sites[0].best_hours[0]).not.toBe(input.sites[1].best_hours[0]);
  });

  it("never reuses records from another parsed response", () => {
    const first = shareForecastHours(snapshot());
    const second = shareForecastHours(snapshot());
    expect(second).toEqual(first);
    expect(second.sites[0].best_hours[0]).not.toBe(
      first.sites[0].best_hours[0],
    );
    expect(second.sites[0].best_hours[0]).toBe(second.sites[1].best_hours[0]);
  });

  it("preserves missing or empty optional data", () => {
    for (const input of [
      null,
      {},
      { sites: [] },
      { sites: [{ id: "empty" }] },
    ]) {
      expect(shareForecastHours(input)).toEqual(input);
    }
  });
});
