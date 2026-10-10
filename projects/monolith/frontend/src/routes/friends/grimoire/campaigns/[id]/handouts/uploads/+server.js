import { error, json } from "@sveltejs/kit";
import { grimoireHeaders } from "$lib/server/grimoire-auth.js";
import { HANDOUT_IMAGE_MAX_BYTES } from "$lib/grimoire/handout.js";

const UUID = /^[0-9a-f-]{36}$/i;
// Multipart framing adds a few hundred bytes around the file itself.
const FRAMING_ALLOWANCE = 16 * 1024;

const tooLarge = () =>
  json({ error: "Handout images can be at most 5 MiB." }, { status: 413 });

// Forward a DM's handout image upload to the backend. The backend sniffs the
// real type and enforces the size cap and the DM role; this only rejects what
// is obviously oversize before buffering it and never forwards a client
// filename or content type of its own.
export async function POST({ request, fetch, cookies, params }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  if (!UUID.test(params.id)) error(404, "Campaign not found.");
  const declared = Number(request.headers.get("content-length"));
  if (declared > HANDOUT_IMAGE_MAX_BYTES + FRAMING_ALLOWANCE) return tooLarge();
  try {
    const file = (await request.formData()).get("file");
    if (!file || typeof file === "string")
      return json({ error: "Choose an image to upload." }, { status: 400 });
    if (file.size > HANDOUT_IMAGE_MAX_BYTES) return tooLarge();
    const form = new FormData();
    form.set("file", file, "handout");
    const response = await fetch(
      `${process.env.API_BASE}/api/grimoire/campaigns/${encodeURIComponent(params.id)}/handouts/uploads`,
      {
        method: "POST",
        body: form,
        signal: AbortSignal.timeout(30_000),
        headers: grimoireHeaders(cookies),
      },
    );
    const body = await response.json().catch(() => null);
    if (!response.ok)
      return json(
        {
          error:
            typeof body?.detail === "string"
              ? body.detail
              : "Could not upload that image.",
        },
        { status: response.status },
      );
    return json(body, {
      status: response.status,
      headers: { "cache-control": "private, no-store" },
    });
  } catch (cause) {
    return json(
      { error: cause.message || "Could not upload that image." },
      { status: 400 },
    );
  }
}
