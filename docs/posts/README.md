# Posts

This directory contains source files for the public `/blog` route. This README
is internal documentation and is never included in the posts manifest.

Each post must follow these conventions:

- Name the file `YYYY-MM-DD-<slug>.md`.
- Start it with YAML frontmatter containing `title` (string), `date`
  (`YYYY-MM-DD`), and `summary` (one sentence). The `date` must match the date
  prefix in the filename.
- `tags` is optional. Its value is a comma-separated string of one to six
  unique tags. Tags are lowercased, may contain lowercase letters, numbers, and
  hyphens, and may be at most 24 characters. Empty items are invalid.
- A `public` key, if present, must be the literal `public: true` or
  `public: false`, with no quotes and no indentation. Anything else is an error.
- Only posts with `public: true` are included in the manifest.
- A missing `public` key, no frontmatter, or `public: false` excludes the post.
- Numbers are point-in-time; posts are never updated.

## Figures

Store post figures as SVG files under `docs/posts/figures/` and reference them
with the exact relative path, for example
`![Memory tiers](figures/memory-tiers.svg)`. The generator validates and embeds
tracked SVG figures so the drawing inherits the blog theme. SVGs need a
`viewBox` and no `width` or `height` attributes. Every figure is followed by a
two-column `Key | Part` table with one row per callout in number order. The
renderer draws that table's key column as the figure's callouts, in the same
circle and tone, using the `data-key` and `data-tone` attributes `figlib.py`
stamps on each toned callout.

Figures use the Haynes workshop-manual register: an exploded view or sectioned
view of the mechanism, with parts separated along one axis and a dashed
assembly line. A part is one outline partitioned by lines that run edge to
edge (a title band, then columns), never a box drawn inside a box. Every part carries a numbered callout. The callout is a circle
of radius 9 with a 1px stroke and a mono 11px number. Leader lines are 1px with
a 2px dot at the part end. Labels use `var(--font-code)` at 11px. Outlines use
`currentColor` at `stroke-width="1.25"`; leaders use `currentColor` at
`stroke-width="1"`. Use `fill="none"` by default and `fill="currentColor"`
only for callout dots. Colour is a fixed vocabulary, one tone per memory tier,
identical in every figure so a reader learns it once: `var(--tone-gpu)` for
the GPU and anything resident in VRAM, `var(--tone-ram)` for pinned host
memory, `var(--tone-cache)` for the page cache, `var(--tone-disk)` for the
NVMe, and `var(--tone-hot)` for the hot expert set. A part takes its tone on
its outline, title, and callout; parts inside it, leaders, and arrows stay
ink so movement reads as movement. The values live in
`technical-drawing.css` for both schemes. Use no other colour, gradients,
filters, shadows, or raster images.

The validator rejects `<style>` elements and `style` attributes (an inlined
`<style>` is document-scoped and CSS escapes defeat a `url(` check), a root
`width` or `height`, and every element or attribute that can load or run
anything. Use presentation attributes only. For text, use
`font-family="ui-monospace, SF Mono, Cascadia Mono, Menlo, monospace"`, which
is what `figlib.py` emits.

An operating sequence explains a cycle as a left-to-right strip of outlined
panels with the same parts held in the same positions. Number each stage and
letter each part, using radius 9 callout circles for both, then list the stages
before the parts in the key table. Solid 1px arrows show movement; dashed 1px
lines show advisory or asynchronous paths.
