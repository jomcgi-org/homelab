# Blog figures

This directory holds tracked SVG source for keyed blog figures. The complete
authoring rules live in `docs/posts/README.md`.

Use a Haynes workshop-manual exploded or sectioned view, numbered callouts,
mono labels, `currentColor` strokes, and at most one `var(--accent-ink)` part.
Follow each figure in its post with a `Key | Part` table in callout order.

The figures here are generated: `figlib.py` holds the primitives (callout,
leader, arrow, hatch, panel) and `build_figures.py` holds one function per
figure. Edit the function and run `python3 docs/posts/figures/build_figures.py`
to regenerate; never hand-edit a generated SVG.
