# Posts

This directory contains source files for the public `/posts` route. This README
is internal documentation and is never included in the posts manifest.

Each post must follow these conventions:

- Name the file `YYYY-MM-DD-<slug>.md`.
- Start it with YAML frontmatter containing `title` (string), `date`
  (`YYYY-MM-DD`), and `summary` (one sentence). The `date` must match the date
  prefix in the filename.
- A `public` key, if present, must be the literal `public: true` or
  `public: false`, with no quotes and no indentation. Anything else is an error.
- Only posts with `public: true` are included in the manifest.
- A missing `public` key, no frontmatter, or `public: false` excludes the post.
- Numbers are point-in-time; posts are never updated.
