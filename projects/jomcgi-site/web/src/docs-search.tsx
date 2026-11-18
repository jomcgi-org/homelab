import { useEffect, useRef, useState } from "react";

type Search = typeof import("./docs-search-index").search;
type Result = ReturnType<Search>[number];

export function DocsSearch() {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<Search>();
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const pending = useRef<Promise<unknown> | null>(null);
  const results: Result[] = search?.(query) ?? [];

  const load = () => {
    if (pending.current || search) return;
    setLoading(true);
    setFailed(false);
    pending.current = import("./docs-search-index")
      .then((module) => setSearch(() => module.search))
      .catch(() => setFailed(true))
      .finally(() => {
        pending.current = null;
        setLoading(false);
      });
  };

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  return (
    <div role="search" className="docs-search">
      <label htmlFor="docs-search">Search documentation</label>
      <input
        id="docs-search"
        ref={input}
        type="search"
        placeholder="Search docs"
        autoComplete="off"
        value={query}
        onFocus={load}
        onChange={(event) => {
          setQuery(event.target.value);
          load();
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setQuery("");
            input.current?.blur();
          }
          if (event.key === "ArrowDown") {
            event.preventDefault();
            list.current?.querySelector("a")?.focus();
          }
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault();
            list.current?.querySelector("a")?.click();
          }
        }}
      />
      <div role="status">
        {loading
          ? "Loading search…"
          : failed
            ? "Search could not load."
            : query.trim()
              ? `${results.length} matches`
              : ""}
      </div>
      {failed && (
        <button type="button" onClick={load}>
          Retry search
        </button>
      )}
      {query.trim() && results.length > 0 && (
        <ul
          ref={list}
          className="docs-results"
          aria-label="Search results"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setQuery("");
              input.current?.focus();
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              const links = [...list.current!.querySelectorAll("a")];
              const current = links.indexOf(
                document.activeElement as HTMLAnchorElement,
              );
              const direction = event.key === "ArrowDown" ? 1 : -1;
              links[
                (current + direction + links.length) % links.length
              ]?.focus();
            }
          }}
        >
          {results.map((result) => (
            <li key={result.slug}>
              <a href={`/docs/${result.slug}`}>
                {result.title}
                <small>
                  {result.project} / {result.kind}
                </small>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
