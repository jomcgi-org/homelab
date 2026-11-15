import type { ComponentProps } from "react";
import { Link } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { keys } from "../api/lobby";
import type { LobbyView } from "../api/schema";
import { clearAccountSheetRecovery } from "../lib/sheet-recovery";
import { gateway } from "./paths";

const gatewayPaths: string[] = Object.values(gateway);

/**
 * Lets every Astryx link (`Link`, `Button href`, `Item href`) navigate through
 * the router. Gateway paths and absolute URLs stay ordinary anchors.
 */
export function RouterLink({
  href = "",
  onClick,
  ...props
}: ComponentProps<"a"> & { href?: string }) {
  const client = useQueryClient();
  const click: ComponentProps<"a">["onClick"] = (event) => {
    onClick?.(event);
    if (event.defaultPrevented || href !== gateway.signOut) return;
    clearAccountSheetRecovery(
      client.getQueryData<LobbyView>(keys.lobby)?.user.id,
    );
  };
  const external =
    /^[a-z][a-z0-9+.-]*:/i.test(href) ||
    href.startsWith("//") ||
    gatewayPaths.some((path) => href.startsWith(path));
  if (external || !href.startsWith("/"))
    return <a href={href} {...props} onClick={click} />;
  return <Link to={href} {...props} onClick={click} />;
}
