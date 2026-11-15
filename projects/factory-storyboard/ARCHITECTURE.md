# Factory storyboard

The browser reads versioned, sanitized snapshots. It has no connection to
private factory APIs. The current package serves invented fixtures in Vite
development; its production build is static and has no snapshot files.

**Why.** [HOME-68](https://factory-plan.jomcgi.dev/factory/issue/HOME-68)
separates the new public contract from the retiring monolith publication
payloads. A mutable manifest points to immutable objects so each publication
can identify its current records, readers can reuse unchanged objects, and
one unavailable transcript does not make a goal unreadable. Hashes establish
byte identity, not publisher authenticity. The later publisher and Worker
own the publication and origin boundaries.

The JSON Schema is the wire contract. The TypeScript types describe the same
records. The browser interprets the schema's supported keywords without
runtime code generation, verifies hashes, and accepts only typed IDs for
links. A withheld turn's schema excludes every content field. Narrative
numbers are filled from the current story stats.

An independent Python test validates the fixtures with draft-07 JSON Schema,
checks their hashes and cross-links, and exercises rejected inputs. Browser
tests also cover the runtime interpreter and rendered pages.

Astryx's built neutral theme and Vite's extracted stylesheet satisfy
`style-src 'self'`. Charts are SVG with a table for each series. No snapshot
text becomes HTML, Markdown, CSS or a URL. Browser routes retain native
anchors and disclosure controls.

| Direction | Work |
| --- | --- |
| Publisher, Garage-to-R2 mirror, narrative agent and Worker | Later slices of [HOME-65](https://factory-plan.jomcgi.dev/factory/issue/HOME-65) |

The [README](README.md) describes the snapshot format, fixture updates and
the Worker serving contract.
