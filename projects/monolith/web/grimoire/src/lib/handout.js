// Handout limits mirror grimoire/handouts.py (HandoutBody, MAX_UPLOAD_BYTES);
// the backend stays authoritative, these only fail fast in the BFF and form.
export const HANDOUT_TITLE_MAX = 200;
export const HANDOUT_MARKDOWN_MAX = 20000;
export const HANDOUT_IMAGE_MAX_BYTES = 5 * 1024 * 1024;

const UUID = /^[0-9a-f-]{36}$/i;
const isUuid = (value) => typeof value === "string" && UUID.test(value);

// The audience picker yields `table` or a list of `pc:<character id>` values
// (the same convention as the narration composer). Turn that into the message
// the session page posts to its state endpoint.
export function composeHandout({ title, markdown, audience, entityId, image }) {
  const choices = audience === "table" ? [] : [audience].flat();
  const message = {
    operation: "handout",
    title: String(title ?? "").trim(),
    markdown: String(markdown ?? ""),
    audience: choices.length ? "pcs" : "table",
    pcIds: choices.map((choice) => String(choice).replace(/^pc:/, "")),
  };
  if (entityId) message.entityId = entityId;
  if (image) message.image = image;
  return message;
}

// BFF side: validate a handout message and build the backend event payload.
export function handoutEvent(input) {
  const title = String(input.title ?? "").trim();
  if (!title || title.length > HANDOUT_TITLE_MAX)
    throw new Error(
      `Give the handout a title of up to ${HANDOUT_TITLE_MAX} characters.`,
    );
  const markdown = String(input.markdown ?? "");
  if (markdown.length > HANDOUT_MARKDOWN_MAX)
    throw new Error(
      `Keep the handout text to ${HANDOUT_MARKDOWN_MAX.toLocaleString("en-US")} characters.`,
    );
  if (!["table", "pcs"].includes(input.audience))
    throw new Error("Choose who receives the handout.");
  const pcIds = input.audience === "pcs" ? (input.pcIds ?? []) : [];
  if (input.audience === "pcs" && !pcIds.length)
    throw new Error("Choose at least one player character.");
  if (!Array.isArray(pcIds) || !pcIds.every(isUuid))
    throw new Error("Invalid player character.");
  const body = { title, markdown };
  if (input.entityId) {
    if (!isUuid(input.entityId)) throw new Error("Invalid knowledge entry.");
    body.entity_id = input.entityId;
  }
  const image = input.image;
  if (image) {
    if (image.source === "upload" && typeof image.key === "string" && image.key)
      body.image = { source: "upload", key: image.key };
    else if (image.source === "chunk" && isUuid(image.chunk_id))
      body.image = { source: "chunk", chunk_id: image.chunk_id };
    else throw new Error("Invalid handout image.");
  }
  return {
    kind: "handout",
    audience: input.audience,
    audience_pc_ids: pcIds,
    body,
    request_id: input.requestId || null,
  };
}

// The only way the UI refers to a handout image: the members-only proxy.
export function handoutImageUrl(campaignId, sessionId, eventId) {
  return `/grimoire/campaigns/${encodeURIComponent(campaignId)}/session/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(eventId)}/image`;
}
