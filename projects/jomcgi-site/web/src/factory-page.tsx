import { useEffect, useState, type MouseEvent } from "react";
import { Heading } from "@astryxdesign/core/Heading";
import { Shell } from "./Shell";
import { Pages, NotAvailable } from "./factory/Pages";
import { useSnapshot } from "./factory/data";
import "./factory/page.css";

/** All deep links hydrate the same pre-rendered shell before reading the URL. */
export function FactoryPage() {
  const { snapshot, error } = useSnapshot();
  const [location, setLocation] = useState("/factory");
  const path = location.split("#")[0]!;
  useEffect(() => {
    const update = () =>
      setLocation(window.location.pathname + window.location.hash);
    update();
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("hashchange", update);
    };
  }, []);
  const ready = snapshot !== null || error;
  useEffect(() => {
    document.title = `${document.querySelector("h1")?.textContent ?? "Factory story"} · Factory · jomcgi.dev`;
  }, [snapshot, path]);
  useEffect(() => {
    if (!ready) return;
    const hash = location.split("#")[1];
    const target =
      (hash && document.getElementById(hash)) ||
      document.getElementById("factory-content");
    target?.focus();
    target?.scrollIntoView?.();
  }, [location, ready]);

  const navigate = (event: MouseEvent<HTMLDivElement>) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const anchor = (event.target as Element).closest("a");
    if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
    const url = new URL(anchor.href);
    if (
      url.origin !== window.location.origin ||
      !/^\/factory(?:\/|$)/.test(url.pathname)
    )
      return;
    event.preventDefault();
    window.history.pushState(null, "", url.pathname + url.search + url.hash);
    setLocation(url.pathname + url.hash);
  };

  return (
    <Shell>
      <div className="factory" onClick={navigate}>
        <a className="skip-link" href="#factory-content">
          Skip to factory content
        </a>
        <div id="factory-content" tabIndex={-1}>
          {snapshot?.story?.notice && (
            <p className="notice">{snapshot.story.notice}</p>
          )}
          {error && (
            <p role="status" className="unavailable">
              Latest snapshot not available.
              {snapshot
                ? " Showing the last available snapshot."
                : " Trying again every minute."}
            </p>
          )}
          {snapshot ? (
            <Pages snapshot={snapshot} path={path} />
          ) : (
            <>
              <header className="lead">
                <Heading level={1}>Factory story</Heading>
                <p>Follow the work from a goal to the change that shipped.</p>
              </header>
              {[
                "Story",
                "Goals",
                "Statistics",
                "Work in progress",
                "Recently shipped",
              ].map((label) => (
                <section key={label}>
                  <Heading level={2}>{label}</Heading>
                  <NotAvailable label={label} />
                </section>
              ))}
            </>
          )}
        </div>
        <div className="factory-footer">
          <a href="/factory">Back to the story</a>
          {snapshot && (
            <p>
              Snapshot:{" "}
              <time dateTime={snapshot.manifest.generatedAt}>
                {snapshot.manifest.generatedAt
                  .replace("T", " ")
                  .replace("Z", " UTC")}
              </time>
            </p>
          )}
        </div>
      </div>
    </Shell>
  );
}
