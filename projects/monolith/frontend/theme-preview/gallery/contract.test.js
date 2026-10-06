import { expect, it } from "vitest";
import {
  CONTENT_STATES,
  DATA_DISPLAY_FIXTURES,
  DEFAULT_LOCALE,
  MEASUREMENT_STATES,
  SERIES_ROLES,
  STATUS_KINDS,
  formatMeasurement,
} from "@homelab/design-system/data-display/core";
import { FIXTURES } from "./fixtures.js";
import { renderFixture } from "./server-render.js";

it("passes every fixture through the package components and core formatter", async () => {
  expect(FIXTURES.locale).toBe(DEFAULT_LOCALE);
  expect(FIXTURES.statuses.map(({ kind }) => kind)).toEqual(
    Object.keys(STATUS_KINDS),
  );
  expect(FIXTURES.states).toEqual(Object.keys(CONTENT_STATES));
  expect(FIXTURES.chart.series.map(({ id }) => id)).toEqual(
    SERIES_ROLES.map(({ id }) => id),
  );
  const { body } = await renderFixture({ fixtures: FIXTURES });
  for (const status of FIXTURES.statuses) expect(body).toContain(status.label);
  for (const row of FIXTURES.rows) expect(body).toContain(row.label);
  for (const measurement of FIXTURES.measurements) {
    const formatted = formatMeasurement(measurement.value, {
      unit: measurement.unit,
      locale: FIXTURES.locale,
    });
    expect(body).toContain(measurement.label);
    expect(body).toContain(formatted.exactText);
    expect(formatted.state).toBe(
      typeof measurement.value === "number" &&
        Number.isFinite(measurement.value)
        ? MEASUREMENT_STATES.AVAILABLE
        : MEASUREMENT_STATES.UNAVAILABLE,
    );
  }
  for (const series of FIXTURES.chart.series) {
    expect(Number.isFinite(series.value)).toBe(true);
    const role = SERIES_ROLES.find(({ id }) => id === series.id);
    expect(series.role).toBe(role.role);
    expect(series.marker).toBe(role.marker);
    expect(body).toContain(series.label);
    expect(body).toContain(
      formatMeasurement(series.value, {
        unit: FIXTURES.chart.units,
        locale: FIXTURES.locale,
      }).exactText,
    );
  }
  expect(formatMeasurement(0).state).toBe(MEASUREMENT_STATES.AVAILABLE);
  for (const missing of [null, undefined, NaN])
    expect(formatMeasurement(missing).state).toBe(
      MEASUREMENT_STATES.UNAVAILABLE,
    );
});

it("pins every kind, state and series role so contract drift requires a gallery update", () => {
  expect(STATUS_KINDS).toEqual({
    ok: {
      label: "OK",
      meaning: "Healthy or successful",
      role: "--ds-ok",
      cue: "✓",
    },
    warn: {
      label: "Warning",
      meaning: "Attention required",
      role: "--ds-warn",
      cue: "△",
    },
    err: { label: "Error", meaning: "Failure", role: "--ds-err", cue: "×" },
    unknown: {
      label: "Unknown",
      meaning: "Not known or unavailable",
      role: "--ds-ink-muted",
      cue: "?",
    },
    pending: {
      label: "Pending",
      meaning: "Waiting or loading",
      role: "--ds-ink-muted",
      cue: "◷",
    },
  });
  expect(CONTENT_STATES).toEqual({
    ready: "Ready",
    loading: "Loading",
    empty: "Empty",
    error: "Error",
    unavailable: "Unavailable",
  });
  expect(
    SERIES_ROLES.map(({ id, role, marker }) => [id, role, marker]),
  ).toEqual([
    ["gpu", "--ds-series-1", "circle"],
    ["host-ram", "--ds-series-2", "square"],
    ["page-cache", "--ds-series-3", "triangle"],
    ["nvme", "--ds-series-4", "diamond"],
    ["hot-expert-set", "--ds-series-5", "cross"],
  ]);
  expect(new Set(FIXTURES.statuses.map(({ kind }) => kind)).size).toBe(
    FIXTURES.statuses.length,
  );
  expect(FIXTURES.measurements).toBe(DATA_DISPLAY_FIXTURES.measurements);
});

it.each([
  [
    "unknown status kind",
    (data) => {
      data.statuses[0].kind = "invented-kind";
    },
    "Unknown status kind",
  ],
  [
    "unknown series id",
    (data) => {
      data.chart.series[0].id = "invented-tier";
    },
    "Unknown series role",
  ],
  [
    "missing chart title",
    (data) => {
      delete data.chart.title;
    },
    "chart title must be non-empty text",
  ],
  [
    "missing chart units",
    (data) => {
      delete data.chart.units;
    },
    "chart units must be non-empty text",
  ],
  [
    "missing chart description",
    (data) => {
      delete data.chart.description;
    },
    "chart description must be non-empty text",
  ],
  [
    "missing status label",
    (data) => {
      delete data.statuses[0].label;
    },
    "status label must be non-empty text",
  ],
  [
    "missing metric label",
    (data) => {
      delete data.measurements[0].label;
    },
    "metric label must be non-empty text",
  ],
  [
    "missing row label",
    (data) => {
      delete data.rows[0].label;
    },
    "row label must be non-empty text",
  ],
  [
    "unknown content state",
    (data) => {
      data.states[1] = "invented-state";
    },
    "Unknown content state",
  ],
  [
    "duplicate series id",
    (data) => {
      data.chart.series[1].id = data.chart.series[0].id;
    },
    "Duplicate series role",
  ],
])(
  "rejects %s using the package's own render-time contract",
  async (_, mutate, message) => {
    // structuredClone preserves undefined, NaN and infinities in edge fixtures.
    const fixtures = structuredClone(FIXTURES);
    mutate(fixtures);
    await expect(renderFixture({ fixtures })).rejects.toThrow(message);
  },
);
