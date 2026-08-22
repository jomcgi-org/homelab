# Posts

This directory contains source files for the public `/posts` route. This README
is internal documentation and is never included in the posts manifest.

Each post must follow these conventions:

- Name the file `YYYY-MM-DD-<slug>.md`.
- Start it with YAML frontmatter containing `title` (string), `date`
  (`YYYY-MM-DD`), `summary` (one sentence), and `public` (`true` or `false`).
- Only posts with `public: true` are included in the manifest.
- A missing `public` key or `public: false` excludes the post.
- Malformed frontmatter on a `public: true` post is a build error.
- Numbers are point-in-time; posts are never updated.

