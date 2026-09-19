// Capture-only metrics used to regenerate the baked public Qwen replay.
// They live beside the capture script so the public recording no longer
// depends on the retired private Qwen demo route.

function nonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function positiveInteger(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
}

function indexedValues(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return null;

  const indexes = Object.keys(value)
    .map(Number)
    .filter((index) => Number.isInteger(index) && index >= 0);
  if (!indexes.length) return [];

  const result = new Array(Math.max(...indexes) + 1);
  for (const index of indexes) result[index] = value[String(index)];
  return result;
}

function profileLayers(profile) {
  return indexedValues(profile?.layers);
}

function profileHits(profile) {
  return indexedValues(profile?.expert_hits);
}

function inferredExpertCount(profile) {
  const rows = profileHits(profile);
  if (!rows) return null;
  const widths = rows
    .filter(Array.isArray)
    .map((row) => row.length)
    .filter((width) => width > 0);
  if (!widths.length || widths.some((width) => width !== widths[0]))
    return null;
  return widths[0];
}

function classifyLayerTier(layers, layerIndex) {
  const values = indexedValues(layers);
  const value = values?.[layerIndex];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "unknown";
  }
  return value > 0 ? "disk" : "resident";
}

function diffExpertHits(previous, current) {
  const previousRows = profileHits(previous);
  const currentRows = profileHits(current);
  if (!previousRows || !currentRows) return [];

  const fired = [];
  currentRows.forEach((currentHits, layer) => {
    const previousHits = previousRows[layer];
    if (!Array.isArray(currentHits) || !Array.isArray(previousHits)) return;
    currentHits.forEach((value, expert) => {
      const before = previousHits[expert];
      if (
        typeof value !== "number" ||
        !Number.isFinite(value) ||
        typeof before !== "number" ||
        !Number.isFinite(before)
      ) {
        return;
      }
      const delta = value - before;
      if (delta > 0) fired.push({ layer, expert, delta });
    });
  });
  return fired;
}

export function calculateTierSummary(profile, geometry) {
  const layers = profileLayers(profile);
  const configuredLayers = positiveInteger(geometry?.num_moe_layers);
  const numLayers = configuredLayers ?? (layers?.length || null);
  const numExperts =
    positiveInteger(geometry?.num_experts) ?? inferredExpertCount(profile);
  const requestedCacheSize = nonNegativeInteger(geometry?.moe_cache_size);
  const bytesPerExpert = positiveInteger(geometry?.unit_bytes?.moe_per_expert);
  const totalExperts =
    numExperts === null || numLayers === null ? null : numExperts * numLayers;

  let residentLayers = null;
  let diskLayers = null;
  if (layers && numLayers !== null && layers.length >= numLayers) {
    residentLayers = 0;
    diskLayers = 0;
    for (let layer = 0; layer < numLayers; layer += 1) {
      const tier = classifyLayerTier(layers, layer);
      if (tier === "resident") residentLayers += 1;
      else if (tier === "disk") diskLayers += 1;
      else {
        residentLayers = null;
        diskLayers = null;
        break;
      }
    }
  }

  const warmExperts =
    residentLayers === null || numExperts === null
      ? null
      : residentLayers * numExperts;
  const diskExperts =
    diskLayers === null || numExperts === null ? null : diskLayers * numExperts;
  const hotExperts =
    diskExperts === null
      ? null
      : diskExperts === 0
        ? 0
        : requestedCacheSize === null
          ? null
          : Math.min(diskExperts, requestedCacheSize);
  const coldExperts =
    diskExperts === null || hotExperts === null
      ? null
      : diskExperts - hotExperts;
  const bytes = (experts) =>
    experts === null || bytesPerExpert === null
      ? null
      : experts * bytesPerExpert;

  return {
    totalExperts,
    residentLayers,
    diskLayers,
    hotExperts,
    warmExperts,
    coldExperts,
    hotBytes: bytes(hotExperts),
    warmBytes: bytes(warmExperts),
    coldBytes: bytes(coldExperts),
  };
}

function likelyHotExperts(profile, geometry) {
  const summary = calculateTierSummary(profile, geometry);
  const layers = profileLayers(profile);
  const hits = profileHits(profile);
  const numExperts =
    positiveInteger(geometry?.num_experts) ?? inferredExpertCount(profile);
  if (
    !layers ||
    !hits ||
    numExperts === null ||
    summary.hotExperts === null ||
    summary.diskLayers === null
  ) {
    return null;
  }

  const diskLayerIndexes = layers
    .map((_, layer) => layer)
    .filter((layer) => classifyLayerTier(layers, layer) === "disk");
  const hot = new Set();
  if (!diskLayerIndexes.length || summary.hotExperts === 0) return hot;

  const perLayer = Math.floor(summary.hotExperts / diskLayerIndexes.length);
  let remainder = summary.hotExperts % diskLayerIndexes.length;
  for (const layer of diskLayerIndexes) {
    const slotCount = Math.min(numExperts, perLayer + (remainder > 0 ? 1 : 0));
    if (remainder > 0) remainder -= 1;
    const row = Array.isArray(hits[layer]) ? hits[layer] : [];
    const ranked = Array.from({ length: numExperts }, (_, expert) => ({
      expert,
      hits:
        typeof row[expert] === "number" && Number.isFinite(row[expert])
          ? row[expert]
          : Number.NEGATIVE_INFINITY,
    })).sort(
      (left, right) => right.hits - left.hits || left.expert - right.expert,
    );
    for (let index = 0; index < slotCount; index += 1) {
      hot.add(`${layer}:${ranked[index].expert}`);
    }
  }
  return hot;
}

export function attributeExpertActivity(previous, current, geometry) {
  const fired = diffExpertHits(previous, current);
  const layers = profileLayers(current);
  const hotExperts = likelyHotExperts(current, geometry);
  const activity = { hotHits: 0, warmHits: 0, coldHits: 0, unknownHits: 0 };

  for (const { layer, expert, delta } of fired) {
    const tier = classifyLayerTier(layers, layer);
    if (tier === "resident") activity.warmHits += delta;
    else if (tier === "disk" && hotExperts?.has(`${layer}:${expert}`)) {
      activity.hotHits += delta;
    } else if (tier === "disk" && hotExperts) activity.coldHits += delta;
    else activity.unknownHits += delta;
  }

  return {
    ...activity,
    totalHits:
      activity.hotHits +
      activity.warmHits +
      activity.coldHits +
      activity.unknownHits,
  };
}
