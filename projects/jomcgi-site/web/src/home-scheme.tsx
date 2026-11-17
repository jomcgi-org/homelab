import { useEffect, useState } from "react";

export function scheme(): "light" | "dark" {
  const chosen = document.documentElement.dataset.homeScheme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function HomeScheme() {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    try {
      const stored = localStorage.getItem("td-theme");
      if (stored === "light" || stored === "dark")
        document.documentElement.dataset.homeScheme = stored;
    } catch {
      /* Storage may be unavailable in private browsing. */
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => setDark(scheme() === "dark");
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  function flip() {
    const next = scheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.homeScheme = next;
    setDark(next === "dark");
    try {
      localStorage.setItem("td-theme", next);
    } catch {
      /* Keep the choice for this view. */
    }
  }
  return (
    <button
      className="scheme"
      type="button"
      onClick={flip}
      aria-label={dark ? "Switch to day scheme" : "Switch to night scheme"}
      title={dark ? "Day" : "Night"}
    >
      <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">
        {dark ? (
          <>
            <circle
              cx="8"
              cy="8"
              r="3.2"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.25"
            />
            <path
              d="M8 1v2M8 13v2M1 8h2M13 8h2M3.1 3.1l1.4 1.4M11.5 11.5l1.4 1.4M3.1 12.9l1.4-1.4M11.5 4.5l1.4-1.4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
            />
          </>
        ) : (
          <path
            d="M10.5 2.2A6 6 0 1 0 13.8 9.5 5 5 0 0 1 10.5 2.2z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinejoin="round"
          />
        )}
      </svg>
    </button>
  );
}
