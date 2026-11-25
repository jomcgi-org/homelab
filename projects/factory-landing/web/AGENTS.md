# AGENTS.md - factory landing web app

Scoped guidance for `projects/factory-landing/web`. The repo-root files still
apply. Build with Astryx and follow the rules in
[`projects/jomcgi-site/web/AGENTS.md`](../../jomcgi-site/web/AGENTS.md): look
each component up with `astryx` before you write one.

This page renders no inline style. `csp.test.tsx` fails on any inline style
element or attribute, so a swizzled or new component must keep that true.
