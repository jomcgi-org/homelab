Design brief for artifact pages

Start from `html` (or an example: call template with example "explainer", "report" or "comparison"). Keep the link tag in the head and the six script tags at the end exactly as they are; they load the house design system and libraries from /_lib/. Add page-only CSS in a <style> after the link; inline scripts are allowed; nothing else loads.

Choose a layout for the content:
- A process or "how X works": hero, steps, one diagram, callouts. (explainer)
- Numbers over time or a status update: hero, stats, charts, timeline. (report)
- A choice between options: hero, compare with one .option.pick, a table, a decision callout. (comparison)
- Anything else: hero, then one idea per section.

Rules:
- One idea per section, with a two-to-six-word h2 and a one-line lede.
- The hero states the point in the h1; the lede says why it matters.
- Show data as a chart, not a paragraph of numbers. Use stat tiles for the 2 to 4 numbers that matter; .stat.accent (the dark readout) marks the headline one.
- Draw diagrams with pre.mermaid (ELK layout handles groups); keep each to one story.
- Icons are Lucide names: <i data-lucide="database"></i> (lucide.dev/icons). Never use emoji as icons or bullets.
- Restraint first: hairlines, whitespace, ink text. Blue is the one accent: use it for one thing per screen (the pick, the current step, the key word). Green means good.
- One bold moment per page, when the content has a single takeaway: p.statement, a large line with the key words in <em> (blue) and an optional <small> explanation.
- Short text: card bodies at most two lines, callouts one or two sentences.
- No external links to assets, fonts or CDNs; they do not load.

Components (classes in house-1.css):
- header.hero: span.eyebrow (icon + label), h1, p.lede, div.meta with span.chip or span.badge
- p.statement with <em> and <small> (at most one per page)
- section.section, div.section-head (h2 + p), div.grid / div.grid.two, div.split (two columns, stacks on phones)
- div.stats > div.stat[.accent] > span.label, span.value, span.delta.up|down
- article.card > span.icon (one <i data-lucide>), h3, p
- ol.steps > li[.now] > h3, p (3 to 6 steps; one row on wide screens; .now marks the current one)
- ol.timeline > li[.done|.now] > time, h3, p
- div.callout[.good|.warn|.bad] > <i data-lucide>, p > strong + text
- div.compare > article.option[.pick] > span.badge (on the pick), h3, p.muted, ul > li.pro|li.con > <i data-lucide="check"|"x"> + text
- figure.figure > pre.mermaid or script.vega-lite (JSON, inline data, no config), figcaption
- div.table-wrap > table.table (td.num for numbers); span.badge.good|warn|bad for states
- pre.code > code; footer.page-foot

The look is jomcgi.dev's. Light and dark follow the reader's setting; motion stops when they prefer reduced motion. Check the page reads at phone width: no wide fixed elements.
