import { Menu } from "./Menu";

export interface NavLink {
  label: string;
  href: string;
}
export interface NavGroup {
  label: string;
  links: readonly NavLink[];
}

/** The page header: the site's name, then one row of links.
 *
 * A group with more than `menuFrom` links folds into a menu, so the row is
 * the same on a phone and a desktop. */
export function SiteHeader({
  brand,
  home = "/",
  label = "Site",
  groups,
  menuFrom = 3,
}: {
  brand: string;
  home?: string;
  /** The accessible name of the navigation. */
  label?: string;
  groups: readonly NavGroup[];
  menuFrom?: number;
}) {
  return (
    <header className="ui-header">
      <a className="ui-brand" href={home}>
        {brand}
      </a>
      <nav aria-label={label}>
        <ul className="ui-nav">
          {groups.map((group) => {
            const links = group.links.map((link) => (
              <li key={link.href}>
                <a href={link.href}>{link.label}</a>
              </li>
            ));
            return links.length > menuFrom ? (
              <li key={group.label}>
                <Menu label={group.label}>{links}</Menu>
              </li>
            ) : (
              links
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
