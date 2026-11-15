// Self-contained because Playwright serializes this function into each document.
// Shared with the hosted fixture; do not close over imports or module state.
export function installPerformanceObservers({
  contentSelector = null,
  contentTimeoutMs = 75000,
} = {}) {
  window.__publicAppPerf = {
    lcp: [],
    shifts: [],
    events: [],
    visibleContentMs: null,
    contentSelector,
    contentSelectorError: null,
    contentObservationIntervalMs: contentSelector ? 100 : null,
    contentObservationStopReason: null,
    supported: PerformanceObserver.supportedEntryTypes,
  };
  const observe = (type, callback, options = {}) => {
    if (PerformanceObserver.supportedEntryTypes.includes(type))
      new PerformanceObserver((list) => callback(list.getEntries())).observe({
        type,
        buffered: true,
        ...options,
      });
  };
  function selectorFor(element) {
    if (!element) return null;
    const parts = [];
    for (
      let node = element;
      node?.nodeType === 1 && parts.length < 6;
      node = node.parentElement
    ) {
      const tag = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${tag}#${CSS.escape(node.id)}`);
        break;
      }
      const siblings = node.parentElement
        ? [...node.parentElement.children].filter(
            (sibling) => sibling.tagName === node.tagName,
          )
        : [node];
      parts.unshift(`${tag}:nth-of-type(${siblings.indexOf(node) + 1})`);
    }
    return parts.join(" > ");
  }
  // IntersectionObserver accounts for viewport and ancestor overflow clipping.
  // A sparse timer covers CSS-only opacity changes that leave the intersection
  // unchanged. It examines only intersecting matches and ends at the cutoff.
  window.__stopPublicAppContentObservation = () => {};
  if (contentSelector) {
    const tracked = new Set();
    const intersecting = new Set();
    let stopped = false;
    let interval;
    let deadline;
    let mutations;
    let intersections;
    function stop(reason) {
      if (stopped) return;
      stopped = true;
      clearInterval(interval);
      clearTimeout(deadline);
      mutations?.disconnect();
      intersections?.disconnect();
      window.__publicAppPerf.contentObservationStopReason = reason;
    }
    window.__stopPublicAppContentObservation = () => stop("cutoff");
    function check() {
      if (stopped) return;
      try {
        for (const element of intersecting) {
          if (
            element.isConnected &&
            element.matches(contentSelector) &&
            element.checkVisibility({
              checkOpacity: true,
              checkVisibilityCSS: true,
            })
          ) {
            window.__publicAppPerf.visibleContentMs = performance.now();
            stop("matched");
            return;
          }
        }
      } catch (error) {
        window.__publicAppPerf.contentSelectorError = error.message;
        stop("error");
      }
    }
    function refreshTargets() {
      if (stopped) return;
      const matches = new Set(document.querySelectorAll(contentSelector));
      for (const element of tracked) {
        if (!matches.has(element)) {
          tracked.delete(element);
          intersecting.delete(element);
          intersections.unobserve(element);
        }
      }
      for (const element of matches) {
        if (!tracked.has(element)) {
          tracked.add(element);
          intersections.observe(element);
        }
      }
    }
    try {
      document.querySelector(contentSelector);
      intersections = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (
              entry.isIntersecting &&
              entry.intersectionRatio > 0 &&
              entry.intersectionRect.width > 0 &&
              entry.intersectionRect.height > 0
            ) {
              intersecting.add(entry.target);
            } else {
              intersecting.delete(entry.target);
            }
          }
          check();
        },
        {
          // Edge adjacency can be isIntersecting=true with zero area. A positive
          // threshold also reports its later transition to visible area.
          threshold: [0, Number.EPSILON],
        },
      );
      mutations = new MutationObserver(refreshTargets);
      mutations.observe(document, {
        subtree: true,
        childList: true,
        attributes: true,
      });
      refreshTargets();
      interval = setInterval(check, 100);
      deadline = setTimeout(() => stop("timeout"), contentTimeoutMs);
    } catch (error) {
      window.__publicAppPerf.contentSelectorError = error.message;
      stop("error");
    }
  }
  observe("largest-contentful-paint", (entries) =>
    window.__publicAppPerf.lcp.push(
      ...entries.map((e) => ({
        startTime: e.startTime,
        size: e.size,
        url: e.url,
        element: e.element?.tagName ?? null,
        selector: selectorFor(e.element),
      })),
    ),
  );
  observe("layout-shift", (entries) =>
    window.__publicAppPerf.shifts.push(
      ...entries.map((e) => ({
        startTime: e.startTime,
        value: e.value,
        hadRecentInput: e.hadRecentInput,
      })),
    ),
  );
  const record = (entries) =>
    window.__publicAppPerf.events.push(
      ...entries.map((e) => ({
        startTime: e.startTime,
        duration: e.duration,
        interactionId: e.interactionId,
        name: e.name,
      })),
    );
  observe("event", record, { durationThreshold: 16 });
  observe("first-input", record);
}
