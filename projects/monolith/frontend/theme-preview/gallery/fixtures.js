import {
  DATA_DISPLAY_FIXTURES,
  DEFAULT_LOCALE,
  SERIES_ROLES,
  STATUS_KINDS,
  formatMeasurement,
} from "@homelab/design-system/data-display/core";

// Presentation inputs only. Render tests pass each input to its package component.
export const SECTIONS = Object.freeze([
  "dashboard",
  "document",
  "controls",
  "navigation",
  "rows",
  "status",
  "metrics",
  "chart",
]);
export const FIXTURES = Object.freeze({
  locale: DEFAULT_LOCALE,
  initialTab: "overview",
  count: 0,
  countUnit: "requests",
  regions: Object.freeze([
    Object.freeze({ value: "north", label: "Invented north" }),
    Object.freeze({ value: "south", label: "Invented south" }),
  ]),
  text: Object.freeze({
    gallery: "Synthetic gallery",
    title: "Synthetic composition gallery",
    introduction:
      "Invented fixtures only. No production page, endpoint or credentials.",
    stateMessage: "Synthetic state, no observations available",
    chartLabel: "Synthetic memory allocation in MiB, exact values in the table",
    legend: "Synthetic memory tier markers",
    exactValues: "exact values",
    tier: "Memory tier",
    allocation: "Exact allocation",
    noObservations: "no observations available.",
    region: "Synthetic region",
    regionDescription: "Choose an invented region",
    reference: "Synthetic reference",
    referenceDescription: "Enter a local sample reference",
    referenceError: "Reference must contain a sheet label",
    destination: "Unavailable destination",
    destinationDescription: "This sample is disabled",
    save: "Save synthetic sheet",
    reset: "Reset fields",
    disabled: "Disabled action",
    submissions: "Submissions",
    dashboard: "Dashboard",
    dashboardDescription: "Synthetic dashboard composition",
    inspect: "Inspect sample",
    views: "Dashboard views",
    observations: "Invented observations",
    plot: "Allocation plot",
    notes: "Synthetic notes for",
    recorded: "recorded",
    document: "Document",
    documentTitle: "Synthetic observation sheet",
    documentDescription: "Document composition with local form state",
    summary: "Observation summary",
    summaryText:
      "This invented sheet records a static set of measurements. The allocation plot uses the ordered package series roles. Every value belongs to this fixture.",
    method: "Method",
    methodText:
      "Read the exact measurements beside compact values. Missing observations remain unavailable, and zero remains a measured value.",
    methodology: "Read synthetic methodology",
    fixedDate: "The fixed date is",
    fixedSource: "No live source supplies these observations.",
    wrappedReference: "Read wrapped reference",
    controls: "Controls",
    expandable: "Synthetic expandable notes",
    interactions:
      "Repeated interactions change only this gallery's local state.",
    toggle: "Toggle notes",
    count: "Count inspection",
    open: "Notes open",
    navigation: "Navigation",
    navigationExamples: "Focused navigation examples",
    panelContent: "invented panel content",
    selected: "Selected",
    metadata: "Dense metadata rows",
    panel: "Panel",
    contentState: "Synthetic content state",
    statuses: "Visible status meanings",
    measurements: "Exact, compact, zero and missing measurements",
    measurement: "Measurement",
    chart: "Chart frame and legend",
    nested: "Dark inset inside light",
    nestedDescription:
      "Explicit inverse boundary, with package roles owned by this inset.",
    nestedZero: "Nested zero",
    nestedAction: "Nested sample action",
    sibling: "Light sibling after dark inset",
    siblingDescription: "These roles inherit from the outer light sheet.",
    unmarked: "Unmarked contract baseline",
    unmarkedAction: "Contract sample action",
  }),
  title: "SyntheticCompositionWithAnUnbrokenReferenceNameForWrapping",
  date: "2026-01-14",
  reference: "invented-sheet-014",
  tabs: Object.freeze(
    [
      { id: "overview", label: "Overview" },
      { id: "notes", label: "Synthetic observation notes" },
      { id: "disabled", label: "Unavailable", disabled: true },
    ].map(Object.freeze),
  ),
  statuses: Object.freeze(
    ["ok", "warn", "err", "unknown", "pending"].map((kind) =>
      Object.freeze({
        kind,
        label: `Synthetic ${STATUS_KINDS[kind].label}: ${STATUS_KINDS[kind].meaning}`,
      }),
    ),
  ),
  states: Object.freeze(["ready", "loading", "empty", "error", "unavailable"]),
  measurements: DATA_DISPLAY_FIXTURES.measurements,
  rows: Object.freeze(
    [
      { label: "Reference", value: "invented-sheet-014" },
      { label: "Recorded date", value: "2026-01-14" },
      {
        label: "LongSyntheticMetadataKeyWithoutBreaks",
        value:
          "InventedMetadataValueWithoutBreaksThatMustRemainReadableOnEveryNarrowSheet",
      },
      {
        label: "Description",
        value:
          "Invented observations across several synthetic memory tiers, with a complete wrapped metadata value.",
      },
      // Numeric rows use the same exact formatter as Metric and the chart table.
      {
        label: "Zero completed",
        value: formatMeasurement(0, {
          unit: "requests",
          locale: DEFAULT_LOCALE,
        }).exactText,
      },
      { label: "Missing observation", value: null },
    ].map(Object.freeze),
  ),
  chart: Object.freeze({
    title: "Synthetic memory allocation",
    units: "MiB",
    description:
      "Invented allocation by memory tier. Markers and labels identify each series; the fallback table gives exact values.",
    series: Object.freeze(
      [
        ["gpu", 80],
        ["host-ram", 64],
        ["page-cache", 48],
        ["nvme", 32],
        ["hot-expert-set", 16],
      ].map(([id, value]) =>
        Object.freeze({
          ...SERIES_ROLES.find((role) => role.id === id),
          id,
          label: `Synthetic ${SERIES_ROLES.find((role) => role.id === id).label}`,
          value,
        }),
      ),
    ),
  }),
});
