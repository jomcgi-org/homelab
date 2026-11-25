# AGENTS.md - jomcgi.dev web app

Scoped guidance for `projects/jomcgi-site/web`. The repo-root files still
apply. Every page here is built from [Astryx](https://github.com/facebook/astryx)
components. Astryx is a beta: read the docs for the installed version, never
recall an API.

## Look a component up before you write one

```sh
astryx search <words>             # components, hooks, docs, templates
astryx --dense component Dialog   # props, variants and examples
astryx template --list            # page and block templates
astryx hook --list
```

In a factory sandbox, run `bash projects/factory/toolchain/bootstrap.sh` and
source the env file it prints. `astryx` is then on `PATH` and answers with the
Core version in `package.json`. Elsewhere, run
`npx @astryxdesign/cli@<Core version> <command>`.

## Rules

- Use an Astryx component for each control, overlay, input and layout. A
  hand-written `onKeyDown` list, dialog or menu needs a reason in the PR.
- When no component fits, run `astryx gap-report` without `--confirm-public`,
  then `astryx swizzle <Name>` and change the ejected copy. `--confirm-public`
  files a public GitHub issue; never pass it without Joe's say. Do not add a
  second component library without Joe's decision.
- Style with the theme tokens (`astryx docs tokens`). Do not hard-code colors
  or spacing.
- Keep `a11y.test.tsx`, `contrast.test.tsx` and `budget.mjs` green.
