export interface PRLink {
  number: number;
  url: string;
}

export interface Status {
  needsYou: {
    key: string;
    title: string;
    priority: string | null;
    url: string;
    createdAt: string;
  }[];
  inFlight: {
    key: string;
    title: string;
    state: string;
    reviewRounds: number;
    pr: (PRLink & { ci: string; review: string; queue: string }) | null;
  }[];
  shipped: {
    key: string;
    title: string;
    pr: PRLink;
    mergedAt: string;
  }[];
  generatedAt: string;
}

// A bad or incompatible response must leave the tool links usable.
export function parseStatus(value: unknown): Status {
  const record = (v: unknown): v is Record<string, unknown> =>
    typeof v === "object" && v !== null;
  const link = (v: unknown) => typeof v === "string" && /^https:\/\//.test(v);
  const pr = (v: unknown) =>
    record(v) && typeof v.number === "number" && link(v.url);
  const item = (v: unknown): v is Record<string, unknown> =>
    record(v) && typeof v.key === "string" && typeof v.title === "string";
  if (
    !record(value) ||
    typeof value.generatedAt !== "string" ||
    !Array.isArray(value.needsYou) ||
    !value.needsYou.every(
      (v) =>
        item(v) &&
        link(v.url) &&
        typeof v.createdAt === "string" &&
        (v.priority === null || typeof v.priority === "string"),
    ) ||
    !Array.isArray(value.inFlight) ||
    !value.inFlight.every(
      (v) =>
        item(v) &&
        typeof v.state === "string" &&
        typeof v.reviewRounds === "number" &&
        (v.pr === null ||
          (record(v.pr) &&
            pr(v.pr) &&
            typeof v.pr.ci === "string" &&
            typeof v.pr.review === "string" &&
            typeof v.pr.queue === "string")),
    ) ||
    !Array.isArray(value.shipped) ||
    !value.shipped.every(
      (v) => item(v) && pr(v.pr) && typeof v.mergedAt === "string",
    )
  )
    throw new Error("Invalid status");
  return value as unknown as Status;
}
