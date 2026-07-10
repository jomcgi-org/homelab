// Shared transform for the /books/{id}/read response, used by both tiers'
// book +page.server.js (first page) and read/+server.js (infinite-scroll
// pages) so the "sign every image_key server-side" step lives in one place.
// Strips image_key from the wire response entirely: the browser only ever
// sees the finished image_url, never the raw bucket-relative key.
import { signedGrimImgUrl } from "./grimoire-img.js";

export function signReadPage(body) {
  return {
    items: (body?.items ?? []).map(signReadItem),
    next_cursor: body?.next_cursor ?? null,
  };
}

function signReadItem(item) {
  if (item.kind !== "image" || !item.image_key) {
    return {
      id: item.id,
      seq: item.seq,
      section_path: item.section_path,
      kind: item.kind,
      content: item.content,
      entities: item.entities ?? [],
    };
  }
  return {
    id: item.id,
    seq: item.seq,
    section_path: item.section_path,
    kind: item.kind,
    content: item.content,
    image_url: signedGrimImgUrl(item.image_key, "display"),
    entities: item.entities ?? [],
  };
}

// Backend base URL, injected by the deployment (API_BASE=http://localhost:8000
// in-pod). No hardcoded fallback: failing loudly beats silently serving from
// the wrong backend (semgrep no-hardcoded-url-fallback).
export function apiBase() {
  const base = process.env.API_BASE;
  if (!base) throw new Error("API_BASE is not set");
  return base;
}
