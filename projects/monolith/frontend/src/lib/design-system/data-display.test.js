import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  DEFAULT_LOCALE,
  MEASUREMENT_STATES,
  formatMeasurement,
  STATUS_KINDS,
  SERIES_ROLES,
  CONTENT_STATES,
  DATA_DISPLAY_FIXTURES,
} from "@homelab/design-system/data-display";

describe("measurement formatting", () => {
  it.each([
    [0, "0", "0 bytes"],
    [-12345, "-12.3K", "-12,345 bytes"],
    [0.125, "0.125", "0.125 bytes"],
    [-12.345, "-12.345", "-12.345 bytes"],
    [999950, "1M", "999,950 bytes"],
    [-999950, "-1M", "-999,950 bytes"],
    [1234567890.1234567, "1.23B", "1,234,567,890.1234567 bytes"],
  ])(
    "formats %s with compact text and the exact measurement",
    (value, text, exactText) => {
      expect(formatMeasurement(value, { unit: "bytes" })).toEqual({
        state: "available",
        text,
        exactText,
        unit: "bytes",
        locale: "en-US",
      });
    },
  );

  it.each([null, undefined, NaN, Infinity, -Infinity, "0", false, {}, [], 0n])(
    "never coerces missing or non-number %s into zero or success",
    (value) => {
      const result = formatMeasurement(value, { unit: "bytes" });
      expect(result.state).toBe(MEASUREMENT_STATES.UNAVAILABLE);
      expect(result.text).toBe("Unavailable");
      expect(result.exactText).toBe("Unavailable");
      expect(result.text).not.toBe("0");
      expect(result.exactText).not.toContain("0");
    },
  );

  it("uses a deterministic locale and preserves fractions down to finite extremes", () => {
    expect(DEFAULT_LOCALE).toBe("en-US");
    expect(formatMeasurement(12345.6789, { locale: "zz-ZZ" })).toEqual(
      formatMeasurement(12345.6789),
    );
    expect(formatMeasurement(12345.6789).exactText).toBe("12,345.6789");
    expect(
      formatMeasurement(12345.6789, { locale: "de-DE", unit: "bytes" })
        .exactText,
    ).toBe("12.345,6789 bytes");
    expect(formatMeasurement(12345.6789, { locale: "de-DE" }).text).toBe(
      new Intl.NumberFormat("de-DE", { maximumFractionDigits: 3 }).format(
        12345.6789,
      ),
    );
    expect(formatMeasurement(12345.6789, { locale: "de-DE" }).text).toBe(
      "12.345,679",
    );
    for (const value of [
      Number.MIN_VALUE,
      Number.MAX_VALUE,
      -Number.MIN_VALUE,
    ]) {
      const formatted = formatMeasurement(value);
      expect(formatted.state).toBe("available");
      expect(formatted.text).not.toBe("0");
      expect(Number(formatted.exactText.replaceAll(",", ""))).toBe(value);
    }
    expect(formatMeasurement(-0).exactText).toBe("-0");
  });

  it("keeps full precision where the locale has no thousands abbreviation", () => {
    expect(formatMeasurement(-12345, { locale: "de-DE" }).text).toBe("-12.345");
    expect(formatMeasurement(-12345, { locale: "de-DE" }).text).not.toBe(
      "-12.300",
    );
    expect(formatMeasurement(1234, { locale: "de-DE" }).text).toBe("1.234");
    expect(formatMeasurement(1234, { locale: "ja-JP" }).text).toBe("1,234");
    expect(formatMeasurement(1234, { locale: "ja-JP" }).text).not.toBe("1230");
    expect(
      formatMeasurement(123456, { locale: "de-DE", unit: "bytes" }).text,
    ).toBe("123.456");
    expect(formatMeasurement(0.000123456).text).not.toBe("0");
  });

  it("validates caller locale and literal units", () => {
    expect(() => formatMeasurement(1, { locale: "" })).toThrow(/locale/);
    expect(() => formatMeasurement(1, { locale: null })).toThrow(/locale/);
    expect(() => formatMeasurement(1, { unit: 4 })).toThrow(/unit/);
    expect(() => formatMeasurement(1, { locale: "invalid_locale" })).toThrow(
      RangeError,
    );
    expect(
      formatMeasurement(1, { unit: "LongUnitWithoutBreaks" }).exactText,
    ).toBe("1 LongUnitWithoutBreaks");
  });
});

describe("stable contracts", () => {
  it("freezes measurement, content and every status meaning with distinct cues", () => {
    expect(MEASUREMENT_STATES).toEqual({
      AVAILABLE: "available",
      UNAVAILABLE: "unavailable",
    });
    expect(Object.keys(CONTENT_STATES)).toEqual([
      "ready",
      "loading",
      "empty",
      "error",
      "unavailable",
    ]);
    expect(Object.keys(STATUS_KINDS)).toEqual([
      "ok",
      "warn",
      "err",
      "unknown",
      "pending",
    ]);
    expect(Object.values(STATUS_KINDS).map(({ role }) => role)).toEqual([
      "--ds-ok",
      "--ds-warn",
      "--ds-err",
      "--ds-ink-muted",
      "--ds-ink-muted",
    ]);
    expect(
      new Set(Object.values(STATUS_KINDS).map(({ cue }) => cue)).size,
    ).toBe(5);
    for (const contract of [MEASUREMENT_STATES, CONTENT_STATES, STATUS_KINDS])
      expect(Object.isFrozen(contract)).toBe(true);
    for (const kind of Object.values(STATUS_KINDS)) {
      expect(Object.isFrozen(kind)).toBe(true);
      expect(kind.label.trim()).not.toBe("");
      expect(kind.meaning.trim()).not.toBe("");
    }
  });

  it("freezes five ordered series meanings and distinct marker shapes", () => {
    expect(SERIES_ROLES.map(({ label }) => label)).toEqual([
      "GPU",
      "Host RAM",
      "Page cache",
      "NVMe",
      "Hot expert set",
    ]);
    expect(SERIES_ROLES.map(({ role }) => role)).toEqual(
      [1, 2, 3, 4, 5].map((i) => `--ds-series-${i}`),
    );
    expect(SERIES_ROLES.map(({ marker }) => marker)).toEqual([
      "circle",
      "square",
      "triangle",
      "diamond",
      "cross",
    ]);
    expect(Object.isFrozen(SERIES_ROLES)).toBe(true);
    expect(SERIES_ROLES.every(Object.isFrozen)).toBe(true);
  });

  it("ships immutable synthetic edge values, wrapping inputs, densities and states", () => {
    const values = DATA_DISPLAY_FIXTURES.measurements.map(({ value }) => value);
    for (const value of [
      0,
      -12345,
      0.125,
      999950,
      1234567890.1234567,
      null,
      undefined,
      NaN,
      Infinity,
      -Infinity,
      "0",
    ])
      expect(values.some((entry) => Object.is(entry, value))).toBe(true);
    expect(DATA_DISPLAY_FIXTURES.densities).toEqual(["dense", "sparse"]);
    expect(DATA_DISPLAY_FIXTURES.states).toEqual([
      "loading",
      "empty",
      "error",
      "unavailable",
    ]);
    expect(
      DATA_DISPLAY_FIXTURES.rows.some(
        ({ label, value, unit }) =>
          label.length > 40 && String(value).length > 40 && unit.length > 40,
      ),
    ).toBe(true);
    expect(Object.isFrozen(DATA_DISPLAY_FIXTURES)).toBe(true);
    for (const group of Object.values(DATA_DISPLAY_FIXTURES)) {
      expect(Object.isFrozen(group)).toBe(true);
      for (const entry of group.filter((value) => typeof value === "object"))
        expect(Object.isFrozen(entry)).toBe(true);
    }
  });

  it("preserves existing CSS exports and imports the core subpath in plain Node without browser globals", () => {
    const pkg = JSON.parse(
      readFileSync(
        new URL("../../../../../design-system/package.json", import.meta.url),
        "utf8",
      ),
    );
    expect(pkg.exports["."]).toBe("./tokens/contract.css");
    expect(pkg.exports["./tokens/contract.css"]).toBe("./tokens/contract.css");
    expect(pkg.exports["./tokens/technical-drawing.css"]).toBe(
      "./tokens/technical-drawing.css",
    );
    // One component entry for every condition: SSR must resolve the same
    // components as the client, so the subpath cannot split on "svelte".
    expect(pkg.exports["./data-display"]).toBe("./data-display/index.js");
    expect(pkg.exports["./data-display/core"]).toBe("./data-display/core.js");
    const output = execFileSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        `
      import assert from 'node:assert/strict';
      for (const name of ['window', 'document', 'navigator']) {
        Object.defineProperty(globalThis, name, { configurable: true, get() { throw new Error('Read browser global: ' + name); } });
      }
      const core = await import('@homelab/design-system/data-display/core');
      assert.equal(core.formatMeasurement(0).text, '0');
      assert.equal(core.DEFAULT_LOCALE, 'en-US');
      assert.equal(core.SERIES_ROLES.length, 5);
      assert(Object.isFrozen(core.STATUS_KINDS));
      console.log('safe');
    `,
      ],
      {
        cwd: fileURLToPath(new URL("../../..", import.meta.url)),
        encoding: "utf8",
      },
    );
    expect(output.trim()).toBe("safe");
  });

  it("keeps styles component-scoped, role-only and wrapping instead of truncating", () => {
    for (const name of [
      "Panel",
      "KeyValue",
      "Status",
      "Metric",
      "ChartFrame",
      "Legend",
    ]) {
      const source = readFileSync(
        new URL(
          `../../../../../design-system/data-display/${name}.svelte`,
          import.meta.url,
        ),
        "utf8",
      );
      const style = source.split("<style>")[1];
      expect(style).toContain("overflow-wrap: anywhere");
      expect(style).not.toMatch(/:global|#[0-9a-f]{3,8}\b|ellipsis/);
      if (name === "Metric" || name === "ChartFrame") {
        // --ds-focus exists only inside a boundary, so disclosures must fall
        // back to the text colour to keep the ring visible outside one.
        expect(style).toContain("var(--ds-focus, currentColor)");
      }
      for (const match of style.matchAll(/var\(([^)]+)\)/g))
        expect(match[1]).toMatch(/^--ds-/);
      expect(source).not.toMatch(
        /\$(?:lib|app)|navigator|window|document|fetch\(/,
      );
    }
    const keyValue = readFileSync(
      new URL(
        "../../../../../design-system/data-display/KeyValue.svelte",
        import.meta.url,
      ),
      "utf8",
    );
    expect(keyValue).toContain("text-align: start");
    expect(keyValue).toContain("@media (max-width: 30rem)");
    expect(keyValue).toContain("grid-template-columns: minmax(0, 1fr);");
  });
});
