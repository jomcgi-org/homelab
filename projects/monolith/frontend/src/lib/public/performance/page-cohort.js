export const PAGE_CACHE_PARAMETER = "__public_perf_page";

// One caller-supplied key identifies a whole measurement cohort, never one run.
// This changes page navigation only; data endpoints keep their existing URLs.
export function pageCohort(key) {
  if (
    key !== undefined &&
    (typeof key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(key))
  )
    throw new Error(
      "--page-cache-key requires 1–96 ASCII letters, digits, _ or -",
    );
  return {
    mode: key === undefined ? "canonical" : "keyed",
    parameter: key === undefined ? null : PAGE_CACHE_PARAMETER,
    key: key ?? null,
  };
}

export function pageRequest(origin, route, cohort = pageCohort()) {
  const url = new URL(route, origin);
  if (url.origin !== new URL(origin).origin)
    throw new Error("Page routes must share --origin");
  if (url.searchParams.has(PAGE_CACHE_PARAMETER))
    throw new Error(
      `Use --page-cache-key instead of a route's ${PAGE_CACHE_PARAMETER}`,
    );
  const canonicalUrl = url.href;
  if (cohort.mode === "keyed")
    url.searchParams.set(PAGE_CACHE_PARAMETER, cohort.key);
  return { route, canonicalUrl, requestedUrl: url.href, pageCache: cohort };
}

// Fragments are not sent in HTTP requests. Every query parameter, including a
// recovery _fresh value or the cohort key, remains part of document identity.
export function sameDocumentUrl(actual, expected) {
  try {
    const left = new URL(actual);
    const right = new URL(expected);
    left.hash = "";
    right.hash = "";
    return left.href === right.href;
  } catch {
    return false;
  }
}

export function pageDestination(finalUrl, cohort) {
  const retained =
    cohort.mode === "keyed"
      ? new URL(finalUrl).searchParams.getAll(PAGE_CACHE_PARAMETER).length ===
          1 &&
        new URL(finalUrl).searchParams.get(PAGE_CACHE_PARAMETER) === cohort.key
      : null;
  return {
    finalUrl,
    finalPageCacheKeyRetained: retained,
    finalPageCacheMode: retained === false ? "key-not-retained" : cohort.mode,
  };
}
