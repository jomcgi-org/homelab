# jomcgi.dev design language

One quiet language for the inner pages, the same on a phone and a desktop.
The home page is the exception: it draws a hardware unit with its own
housing tokens (`--home-*`). `src/site.css` and `src/palette.css` hold the
values; this file states the rules.

## Type

Inter is the one family. Size and tight tracking carry the hierarchy.

| Role | Size | Weight |
| --- | --- | --- |
| Title (h1) | 2rem to 3.25rem | 600, tight |
| Section (h2) | 1.25rem | 600 |
| Post section (blog prose h2) | 1.5rem, more space above than below | 600 |
| Heading (h3, h4) | 1rem | 600 |
| Body | 1rem, line height 1.6, measure 68ch | 400 |
| Small | 0.875rem, for supporting text | 400 |

No text is smaller than 12px, including chart labels and home key legends.

## Colour

- Ink on a near-white page (`--color-background-body`). The dark scheme is
  the same with light ink on near-black. Each token sets both through
  `light-dark()`.
- Hairlines (`--color-border`) separate content. Nothing is boxed in ink.
- Links are ink and underlined. The current nav item is underlined, so
  colour is never the only mark.
- Blue is the one accent: the focus outline and Factory's in-review work.
  Data palettes (`--lb-provider-*`, `--color-stars-*`, `--hikes-effort-*`)
  are for charts and maps only.
- A map or chart follows the page scheme. Stars uses the OpenFreeMap
  `liberty` style on a light page and `dark` on a dark page.

## Space and shape

- The page gutter is 1rem on a phone and 1.5rem above it.
- The title is the first thing under the nav. Secondary navigation never
  pushes the title off a phone screen: it becomes one row.
- Controls and chips share the soft corner (`--radius-element`, 6px).
  Shadows are off.

## Rules

- Use Astryx components and the tokens above (`AGENTS.md`). Do not
  hard-code a colour or a size outside `palette.css`.
- `a11y.test.tsx`, `contrast.test.tsx` and the browser tests check both
  schemes.
