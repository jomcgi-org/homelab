// Pure scrub math for the public "boot once, restore forever" scroll story.
// No DOM, no Svelte: FcScrollStory.svelte feeds it a scroll fraction t and a
// set of element refs, and applies the results imperatively (the same
// ExploreCanvas / grimoire ScrollStory discipline). Ported from the approved
// reference mockup at ./reference-mockup.html so the choreography survives
// the move to Svelte.
//
// Everything here is a pure function of its arguments: the scrub timing and
// the bar-segment math are the only genuinely unit-testable parts, so they
// live away from the 60fps DOM writes and get tested directly against the
// real baked trace data in timeline.test.js.

export const clamp = (v, a, b) => Math.min(Math.max(v, a), b);

export const lerp = (a, b, t) => a + (b - a) * t;

// Map a master fraction t into a sub-window [a, b], clamped to 0..1. Named
// `sub` to match the grimoire timeline.js convention (the mockup called this
// `seg`, but "seg" collides with the bar "segment" vocabulary used throughout
// this module, so `sub` was kept instead for clarity).
export const sub = (t, a, b) => clamp((t - a) / (b - a), 0, 1);

// Easing curves, named as in the mockup: `easeInOut` is the default smooth
// in/out (mockup's `ez`), `easeOut` is a cubic ease-out (mockup's `eo`).
export const easeInOut = (t) =>
  t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
export const easeOut = (t) => 1 - (1 - t) ** 3;

// Each beat owns its own window on the master timeline t in [0, 1]. Unlike
// the grimoire PHASES these windows are NOT contiguous: the mockup leaves
// gaps between beats (e.g. heroOut ends at 0.07, build starts at 0.08) as
// breathing room in the scrub. Ported verbatim from the mockup's `P` object.
export const PHASES = {
  heroOut: [0.0, 0.07],
  build: [0.08, 0.3],
  freeze: [0.32, 0.52],
  restore: [0.54, 0.76],
  repeat: [0.78, 0.92],
  out: [0.93, 1.0],
};

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

// Caption fade math: fades a caption in over [a, a+0.05] and out over
// [b-0.04, b], returning both the opacity (0..1) and the eased "entrance"
// progress used to drive the caption's translateY settle. Ported from the
// mockup's `cap(elm, t, a, b)`; split into a pure function returning values
// instead of mutating a DOM element directly.
export function captionOpacity(t, a, b) {
  const inn = sub(t, a, a + 0.05);
  const out = 1 - sub(t, b - 0.04, b);
  const opacity = Math.min(inn, out);
  const entrance = easeOut(inn);
  return { opacity, entrance };
}
