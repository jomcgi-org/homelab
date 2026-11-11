import type { ComponentProps } from "react";
import { Link } from "react-router";
import { gateway } from "./paths";

const gatewayPaths: string[] = Object.values(gateway);

/**
 * Lets every Astryx link (`Link`, `Button href`, `Item href`) navigate through
 * the router. Gateway paths and absolute URLs stay ordinary anchors.
 */
export function RouterLink({
  href = "",
  ...props
}: ComponentProps<"a"> & { href?: string }) {
  const external =
    /^[a-z][a-z0-9+.-]*:/i.test(href) ||
    href.startsWith("//") ||
    gatewayPaths.some((path) => href.startsWith(path));
  if (external || !href.startsWith("/")) return <a href={href} {...props} />;
  return <Link to={href} {...props} />;
}
