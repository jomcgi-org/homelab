let nextProtocol = 0;

// Use MapLibre's public protocol hook to observe the actual HTTP result before
// a source can convert a missing tile into an empty, apparently loaded tile.
// Each map owns its registration and request generation. The browser still
// fetches the original HTTPS URL once, with its normal HTTP cache semantics.
export function observeMapRequests(
  maplibre,
  { onFailure, optionalTilePrefix },
) {
  const protocol = `trip-tiles-${++nextProtocol}`;
  let generation = 0;
  let active = true;
  const requests = new Set();

  maplibre.addProtocol(protocol, async (parameters, controller) => {
    const wrapped = new URL(parameters.url);
    const requestGeneration = Number(wrapped.hostname);
    const url = decodeURIComponent(wrapped.pathname.slice(1));
    if (!active || requestGeneration !== generation) controller.abort();
    controller.signal.throwIfAborted();
    requests.add(controller);
    try {
      const response = await fetch(url, {
        method: parameters.method || "GET",
        body: parameters.body,
        headers: parameters.headers,
        credentials: parameters.credentials,
        cache: parameters.cache,
        referrerPolicy: parameters.referrerPolicy,
        signal: controller.signal,
      });
      controller.signal.throwIfAborted();
      if (!response.ok) {
        throw new maplibre.AJAXError(
          response.status,
          response.statusText,
          url,
          await response.blob(),
        );
      }
      const data = await response.arrayBuffer();
      controller.signal.throwIfAborted();
      return {
        data,
        cacheControl: response.headers.get("Cache-Control"),
        expires: response.headers.get("Expires"),
        etag: response.headers.get("ETag"),
      };
    } catch (error) {
      if (
        !active ||
        requestGeneration !== generation ||
        controller.signal.aborted
      ) {
        controller.abort();
        controller.signal.throwIfAborted();
      }
      if (
        active &&
        requestGeneration === generation &&
        !controller.signal.aborted &&
        error?.name !== "AbortError"
      )
        onFailure(error);
      throw error;
    } finally {
      requests.delete(controller);
    }
  });

  function reset() {
    generation++;
    for (const controller of requests) controller.abort();
    requests.clear();
  }

  return {
    transformRequest(url, type) {
      if (
        type !== "Tile" ||
        (optionalTilePrefix && url.startsWith(optionalTilePrefix))
      )
        return { url };
      return { url: `${protocol}://${generation}/${encodeURIComponent(url)}` };
    },
    reset,
    dispose() {
      if (!active) return;
      active = false;
      reset();
      maplibre.removeProtocol(protocol);
    },
  };
}
