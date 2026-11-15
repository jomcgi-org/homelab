// Track top-level document requests separately from redirects and child frames.
// A second request can reset browser performance.timeOrigin while the CDP and
// context inventories still contain the first page. Keep it, but never combine
// those clocks into a comparable load sample.
export function createNavigationTracker(mainFrameId) {
  const navigations = [];
  let start;
  return {
    request(event) {
      if (event.type !== "Document" || event.frameId !== mainFrameId) return;
      start ??= event.timestamp;
      let navigation = navigations.find((n) => n.requestId === event.requestId);
      if (!navigation) {
        navigation = {
          requestId: event.requestId,
          frameId: event.frameId,
          startMs: (event.timestamp - start) * 1000,
          urls: [],
        };
        navigations.push(navigation);
      }
      navigation.urls.push(event.request.url);
    },
    response(event, headers = {}) {
      const navigation = navigations.find(
        (n) => n.requestId === event.requestId,
      );
      if (!navigation) return;
      navigation.responseReceivedMs = (event.timestamp - start) * 1000;
      navigation.status = event.response.status;
      navigation.headers = headers;
    },
    get start() {
      return start;
    },
    isInitialDocument(event) {
      return (
        event.type === "Document" &&
        event.frameId === mainFrameId &&
        event.requestId === navigations[0]?.requestId
      );
    },
    snapshot() {
      return {
        navigations: structuredClone(navigations),
        navigationCount: navigations.length,
        additionalDocumentNavigation: navigations.length > 1,
        comparisonEligible: navigations.length === 1,
        comparisonExclusion:
          navigations.length === 1
            ? null
            : navigations.length > 1
              ? "multiple top-level document requests reset the observation clock"
              : "no top-level document request observed",
      };
    },
  };
}
