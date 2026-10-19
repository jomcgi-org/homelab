// Canvas paint is imperative. CSS inheritance alone cannot repaint it when
// SchemeToggle changes the root attribute or the system preference changes.
export function observeScheme(repaint) {
  const observer = new MutationObserver(repaint);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", repaint);
  return () => {
    observer.disconnect();
    media.removeEventListener("change", repaint);
  };
}
