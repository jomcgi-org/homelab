# Shared UI

`@homelab/ui`: React components for sites built on the Astryx theme. A page
imports a component and writes no styling of its own.

## Rules

| Rule                                                                      | Why                                                       |
| ------------------------------------------------------------------------- | --------------------------------------------------------- |
| Use an Astryx component when one fits the three limits below.             | Less code of our own.                                     |
| Otherwise write the component here, styled only with Astryx theme tokens. | It follows any theme, and a new project gets it for free. |
| No inline styles.                                                         | The sites send `style-src 'self'`.                        |
| A link or text the reader needs is in the HTML before any script runs.    | The sites pre-render, and are tested without script.      |
| Measure the script a component adds.                                      | The sites hold each page under 150 KiB.                   |
| No colour values in `ui.css`.                                             | The theme owns colour (`palette.css` in a site).          |

## Components

| Component       | Use                                                                     |
| --------------- | ----------------------------------------------------------------------- |
| `SiteHeader`    | The site's name and one row of links. A long group folds into a `Menu`. |
| `Menu`          | A native `<details>` menu of links. Opens with no script.               |
| `Chips`, `Chip` | A wrapping row of tags or filters. `current` marks the selected one.    |
| `Skeleton`      | Holds the place of loading content so nothing shifts.                   |
| `StateMark`     | A small square and a word: `done`, `active`, `started`, `idle`.         |

## Use

```tsx
import { SiteHeader } from "@homelab/ui";
import "@homelab/ui/ui.css";
```

The package ships source. The site's bundler compiles it, so there is no
build step here.
