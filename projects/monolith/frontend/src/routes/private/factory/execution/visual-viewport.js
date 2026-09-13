export function setupVisualViewport(
  window,
  element,
  { measure, apply },
  mobileMediaQuery = "(max-width: 760px)",
) {
  const viewport = window.visualViewport;
  if (!viewport) return () => {};

  const mediaQuery = window.matchMedia(mobileMediaQuery);
  let viewportSubscribed = false;

  function handleViewportResize() {
    const measured = measure();
    element.style.setProperty("--console-h", `${viewport.height}px`);
    if (viewport.offsetTop !== 0) window.scrollTo(0, 0);
    apply(measured);
  }

  function handleViewportScroll() {
    const measured = measure();
    element.style.setProperty("--console-h", `${viewport.height}px`);
    apply(measured);
  }

  function subscribeViewport() {
    if (viewportSubscribed) return;

    const heightChanged = element.clientHeight !== viewport.height;
    const measured = heightChanged ? measure() : undefined;
    element.style.setProperty("--console-h", `${viewport.height}px`);
    if (heightChanged) apply(measured);
    viewport.addEventListener("resize", handleViewportResize);
    viewport.addEventListener("scroll", handleViewportScroll);
    viewportSubscribed = true;
  }

  function unsubscribeViewport() {
    if (!viewportSubscribed) return;

    viewport.removeEventListener("resize", handleViewportResize);
    viewport.removeEventListener("scroll", handleViewportScroll);
    viewportSubscribed = false;
  }

  function handleMediaChange(event) {
    if (event.matches) {
      subscribeViewport();
      return;
    }

    unsubscribeViewport();
    element.style.removeProperty("--console-h");
  }

  mediaQuery.addEventListener("change", handleMediaChange);
  if (mediaQuery.matches) subscribeViewport();

  return () => {
    unsubscribeViewport();
    mediaQuery.removeEventListener("change", handleMediaChange);
  };
}
