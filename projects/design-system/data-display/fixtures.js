/** Synthetic, deterministic values only. No telemetry or application contracts. */
const measurements = [
  { label: "Zero", value: 0, unit: "requests" },
  { label: "Negative", value: -12345, unit: "bytes" },
  { label: "Fractional", value: 0.125, unit: "seconds" },
  { label: "Large rounded value", value: 999950, unit: "bytes" },
  { label: "Large fractional value", value: 1234567890.1234567, unit: "bytes" },
  { label: "Missing null", value: null, unit: "bytes" },
  { label: "Missing undefined", value: undefined, unit: "bytes" },
  { label: "Not a number", value: NaN, unit: "bytes" },
  { label: "Positive infinity", value: Infinity, unit: "bytes" },
  { label: "Negative infinity", value: -Infinity, unit: "bytes" },
  { label: "Non-number input", value: "0", unit: "bytes" },
  {
    label: "LongSyntheticMeasurementNameWithoutBreaksForWrapping",
    value: 1234.56789,
    unit: "LongSyntheticUnitWithoutBreaksForWrapping",
  },
];
const rows = [
  { label: "Name", value: "Synthetic host" },
  {
    label: "LongSyntheticLabelWithoutBreaksForWrapping",
    value: "LongSyntheticValueWithoutBreaksForWrapping",
    unit: "LongSyntheticUnitWithoutBreaksForWrapping",
  },
  { label: "Zero", value: 0, unit: "requests" },
  { label: "Missing", value: null, unit: "bytes" },
];

export const DATA_DISPLAY_FIXTURES = Object.freeze({
  measurements: Object.freeze(measurements.map(Object.freeze)),
  rows: Object.freeze(rows.map(Object.freeze)),
  densities: Object.freeze(["dense", "sparse"]),
  states: Object.freeze(["loading", "empty", "error", "unavailable"]),
});
