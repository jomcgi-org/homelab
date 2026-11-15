import { useRef, type ComponentProps } from "react";
import { Link } from "react-router";
import { useToast } from "@astryxdesign/core/Toast";
import { readSignOutAccount } from "../api/lobby";
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
  const toast = useToast();
  const pending = useRef(false);
  const click: ComponentProps<"a">["onClick"] = async (event) => {
    onClick?.(event);
    if (event.defaultPrevented || href !== gateway.signOut) return;
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    try {
      const accountId = await readSignOutAccount();
      if (accountId) clearAccountSheetRecovery(accountId);
      location.assign(gateway.signOut);
    } catch {
      // An unknown identity is never permission to erase another account's work.
      pending.current = false;
      toast({
        type: "error",
        body: "Could not verify your account for sign-out. Try again.",
      });
    }
  };
  const external =
    /^[a-z][a-z0-9+.-]*:/i.test(href) ||
    href.startsWith("//") ||
    gatewayPaths.some((path) => href.startsWith(path));
  if (external || !href.startsWith("/"))
    return <a href={href} {...props} onClick={click} />;
  return <Link to={href} {...props} onClick={click} />;
}
