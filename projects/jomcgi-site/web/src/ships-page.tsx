import { Shell } from "./Shell";
import { useEffect, useRef } from "react";

export function ShipsPage() {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    let cleanup: (() => void) | undefined;
    import("./ships/main.js").then(({ showMap }) => {
      if (active && container.current) cleanup = showMap(container.current);
    });
    return () => {
      active = false;
      cleanup?.();
    };
  }, []);
  return (
    <Shell>
      <h1>Ships</h1>
      <div ref={container} />
    </Shell>
  );
}
