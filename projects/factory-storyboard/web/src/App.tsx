import { useEffect, useState } from "react";
import type { MouseEvent } from "react";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { Pages } from "./Pages";
import { useSnapshot } from "./data";

const themeKey = "factory-storyboard-theme";
function initialTheme(): "light" | "dark" {
  try {
    const saved = localStorage.getItem(themeKey);
    if (saved === "light" || saved === "dark") return saved;
  } catch { /* Storage may be unavailable in a read-only browser. */ }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function App() {
  const { snapshot, error } = useSnapshot();
  const [location, setLocation] = useState(() => window.location.pathname + window.location.hash);
  const [scheme, setScheme] = useState(initialTheme);
  const path = location.split("#")[0]!;
  useEffect(() => {
    const update = () => setLocation(window.location.pathname + window.location.hash);
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    return () => { window.removeEventListener("popstate", update); window.removeEventListener("hashchange", update); };
  }, []);
  useEffect(() => {
    try { localStorage.setItem(themeKey, scheme); } catch { /* The theme still works for this visit. */ }
  }, [scheme]);
  const ready = snapshot !== null || error;
  useEffect(() => {
    if (ready) document.title = `${document.querySelector("h1")?.textContent ?? "Factory"} · Homelab`;
  }, [snapshot, path, ready]);
  useEffect(() => {
    if (!ready) return;
    const hash = location.split("#")[1];
    const target = (hash && document.getElementById(hash)) || document.getElementById("main");
    target?.focus();
    target?.scrollIntoView?.();
  }, [location, ready]);
  const navigate = (event: MouseEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = (event.target as Element).closest("a");
    if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
    const url = new URL(anchor.href);
    if (url.origin !== window.location.origin) return;
    if (url.pathname === window.location.pathname && url.hash) return;
    event.preventDefault();
    window.history.pushState(null, "", url.pathname + url.hash);
    setLocation(url.pathname + url.hash);
  };
  return <Theme theme={neutralTheme} mode={scheme}>
    <div className="storyboard" onClick={navigate}>
      <a className="skip-link" href="#main">Skip to content</a>
      <div className="site-controls"><a href="/">Factory story</a><button type="button" onClick={() => setScheme(scheme === "light" ? "dark" : "light")}>Use {scheme === "light" ? "dark" : "light"} theme</button></div>
      <main id="main" className="td sheet" tabIndex={-1}>
        {snapshot?.story?.notice && <p className="notice">{snapshot.story.notice}</p>}
        {error && <p role="status" className="unavailable">Latest snapshot not available.{snapshot ? " Showing the last available snapshot." : " Trying again every minute."}</p>}
        {snapshot ? <Pages snapshot={snapshot} path={path} /> : <h1>{error ? "Factory story not available" : "Loading the factory story…"}</h1>}
      </main>
      <footer><a href="/">Back to the story</a>{snapshot && <p>Snapshot: <time dateTime={snapshot.manifest.generatedAt}>{snapshot.manifest.generatedAt.replace("T", " ").replace("Z", " UTC")}</time></p>}</footer>
    </div>
  </Theme>;
}
