// Turn a relationship edge into a short reading phrase for the World codex,
// so the relationship list reads as sentences ("owns the Sunsword", "ally of
// the Keepers") instead of arrow glyphs.
//
// An edge is an undirected pair {from, to, rel_type} (see grimoire/explore.py's
// ego projection). Direction relative to the focused entity is what decides the
// wording:
//   - outgoing (edge.from === focusId): the focus is the subject, so the verb
//     leads and the peer is the object -> "owns <peer>".
//   - incoming (edge.to === focusId): the peer is the subject and the focus is
//     the object -> "<peer> owns this".
//   - symmetric rel_type: order carries no meaning, so both directions read the
//     same, peer-trailing form -> "ally of <peer>".
//
// rel_type arrives UPPER_SNAKE_CASE from the backend (extract.py's closed
// vocabulary, stored verbatim); everything here lower-cases first so callers
// never have to.

// rel_types whose two endpoints are interchangeable: the edge means the same
// thing read either way, so we never say "<peer> X this", always "X <peer>".
// Sourced from the `symmetric=True` signatures in grimoire/extract.py's
// REL_SIGNATURES (NEAR, CONNECTS_TO, ALLY_OF, ENEMY_OF, VARIANT_OF, SIBLING_OF,
// SPOUSE_OF, RELATED_TO), plus ASSOCIATED_WITH: not in the current closed
// vocabulary, but a natural symmetric fallback the model or a future vocab
// revision may emit, and harmless to treat as symmetric if it never appears.
// Compared lower-cased, so membership is case-insensitive.
export const SYMMETRIC = new Set([
  "near",
  "connects_to",
  "ally_of",
  "enemy_of",
  "variant_of",
  "sibling_of",
  "spouse_of",
  "related_to",
  "associated_with",
]);

// snake_case -> spaced words, lower-cased. "OWNS" -> "owns", "located_in" ->
// "located in", "ALLY_OF" -> "ally of".
export function humanize(relType) {
  return String(relType ?? "")
    .toLowerCase()
    .replace(/_/g, " ")
    .trim();
}

export function isSymmetric(relType) {
  return SYMMETRIC.has(String(relType ?? "").toLowerCase());
}

// Build {pre, peer, post} display fragments for one edge relative to `focusId`.
// `peer` is always the raw peer name (the caller renders it as the clickable
// link); `pre`/`post` are the surrounding phrase text, one of which is empty.
// An unknown rel_type is not special-cased: it falls through to the same
// directional handling as any known asymmetric type, so a future vocabulary
// addition reads sensibly ("<verb> <peer>" / "<peer> <verb> this") without a
// code change here.
export function phrase({ focusId, edge, peerName }) {
  const verb = humanize(edge?.rel_type);
  const peer = peerName ?? "";
  const outgoing = edge?.from === focusId;

  if (isSymmetric(edge?.rel_type)) {
    // Order carries no meaning: always the peer-trailing form.
    return { pre: verb ? `${verb} ` : "", peer, post: "" };
  }
  if (outgoing) {
    // Focus is the subject: "owns <peer>".
    return { pre: verb ? `${verb} ` : "", peer, post: "" };
  }
  // Incoming: peer is the subject, focus is the object: "<peer> owns this".
  return { pre: "", peer, post: verb ? ` ${verb} this` : "" };
}
