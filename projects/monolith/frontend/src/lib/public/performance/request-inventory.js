import {
  responseTransferSize,
  responseTransferTotals,
  transferTotals,
  workerTransferSize,
} from "./metrics.js";

export function observeContextRequests(context) {
  const requests = new Map();
  context.on("request", (request) => {
    const type = request.resourceType();
    requests.set(request, {
      url: request.url(),
      type: type[0].toUpperCase() + type.slice(1),
      complete: false,
      transferBytes: null,
    });
  });
  context.on("response", (response) => {
    const item = requests.get(response.request());
    if (item) item.status = response.status();
  });
  context.on("requestfinished", (request) => {
    const item = requests.get(request);
    if (!item) return;
    item.complete = true;
    item.sizing = responseTransferSize(request);
  });
  context.on("requestfailed", (request) => {
    const item = requests.get(request);
    if (item) {
      item.complete = true;
      item.failed = request.failure()?.errorText ?? "request failed";
    }
  });
  return requests;
}

export async function withinDeadline(promise, timeoutMs, message) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

// Shared by the ordinary cutoff and the error path. Never replace a successful
// cutoff with later traffic. Only sizing already-finished requests may wait;
// unfinished requests stay incomplete, with any received HTTP status retained.
export async function ensureRequestInventory(
  result,
  contextRequests,
  pageRequests,
  otherTargets,
  phase,
  timeoutMs = 5000,
) {
  if (result.requests) return;
  const snapshot = [...contextRequests].map((item) => ({ ...item }));
  const workers = new Set(
    otherTargets
      .filter((target) => target.type === "worker")
      .map((target) => target.url),
  );
  result.inventoryPhase = phase;
  result.inventoryCapturedAt = new Date().toISOString();
  result.pageTargetRequests = structuredClone(pageRequests);
  result.pageTargetTotals = transferTotals(result.pageTargetRequests);
  result.requests = await Promise.all(
    snapshot.map(async ({ sizing, ...item }) => {
      if (!sizing) return item;
      try {
        const size = await withinDeadline(
          sizing,
          timeoutMs,
          "request.sizes timed out",
        );
        return workerTransferSize({ ...item, ...size }, workers);
      } catch (error) {
        return { ...item, transferBytes: null, sizeError: error.message };
      }
    }),
  );
  Object.assign(result, responseTransferTotals(result.requests));
}
