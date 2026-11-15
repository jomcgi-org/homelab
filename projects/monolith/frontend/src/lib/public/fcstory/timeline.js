// Pure trace-bar calculations for cold-build segments and restore widths.

// Minimum rendered width (percent of bar width) for a bar segment, so a
// genuinely tiny phase (e.g. the ~15ms firecracker_boot sliver inside a
// 9,264ms cold build) still reads as a visible sliver instead of vanishing
// at sub-pixel width. Mirrors the mockup's inline `min-width: 2px` escape
// hatch, expressed here as a floor on the fractional width instead of a
// pixel value (the component applies the pixel floor in the DOM).
export const MIN_SEGMENT_FRACTION = 0.004;

// Cold build bar: turn `cold.phases` (real trace data, names
// firecracker_boot / guest_wait_ready / snapshot_save) into segment
// fractions of `cold.total`, each floored at MIN_SEGMENT_FRACTION so no
// phase silently disappears. Returns [{ name, fraction, ms }], in the same
// order as the input phases. Fractions do not necessarily sum to exactly 1
// when the floor kicks in (the visual gets a hair wider than 100%, same
// trade the mockup makes with its literal min-width px floor).
export function coldSegments(cold) {
  return cold.phases.map((p) => ({
    name: p.name,
    ms: p.ms,
    fraction: Math.max(p.ms / cold.total, MIN_SEGMENT_FRACTION),
  }));
}

// Restore-vs-cold bar: the width fraction (of the full track width) that a
// restore run's total should occupy so cold and restore bars sit "same
// scale" beside each other, floored the same way as coldSegments so the
// restore sliver stays visible.
export function restoreBarFraction(restore, cold) {
  return Math.max(restore.total / cold.total, MIN_SEGMENT_FRACTION);
}
