import { error, json } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";
import { sessionState } from "$lib/server/grimoire-session.js";

export async function GET({ fetch, cookies, params, url }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  try {
    if (url.searchParams.has("inventory")) {
      const view = url.searchParams.get("inventory");
      if (!["items", "changes"].includes(view))
        throw new Error("Invalid inventory view.");
      let path = `/campaigns/${params.id}/inventory`;
      if (view === "changes") {
        path += "/changes";
        const item = url.searchParams.get("item");
        if (item !== null) {
          if (!/^[0-9a-f-]{36}$/i.test(item)) throw new Error("Invalid item.");
          path += `?item_id=${encodeURIComponent(item)}`;
        }
      }
      return json(await grimoireJson(fetch, cookies, path), {
        headers: { "cache-control": "private, no-store" },
      });
    }
    if (url.searchParams.has("entity")) {
      const id = url.searchParams.get("entity");
      if (!/^[0-9a-f-]{36}$/i.test(id))
        throw new Error("Invalid knowledge entry.");
      return json(
        await grimoireJson(
          fetch,
          cookies,
          `/campaigns/${params.id}/entities/${id}`,
        ),
        { headers: { "cache-control": "private, no-store" } },
      );
    }
    if (url.searchParams.has("notes")) {
      const kind = url.searchParams.get("notes");
      if (!["character", "party"].includes(kind))
        throw new Error("Invalid notes tab.");
      return json(
        (
          await grimoireJson(
            fetch,
            cookies,
            `/campaigns/${params.id}/notes?kind=${kind}`,
          )
        ).map((note) => ({
          ...note,
          editable: note.can_edit,
          links: {
            ...note.links,
            entity_ids: note.links.entities.map((entity) => entity.id),
            entity_names: Object.fromEntries(
              note.links.entities.map((entity) => [entity.id, entity.name]),
            ),
          },
        })),

        { headers: { "cache-control": "private, no-store" } },
      );
    }
    if (url.searchParams.has("q")) {
      const query = new URLSearchParams({
        q: url.searchParams.get("q"),
        limit: "30",
      });
      if (url.searchParams.get("notGrantedTo"))
        query.set("not_granted_to", url.searchParams.get("notGrantedTo"));
      return json(
        await grimoireJson(
          fetch,
          cookies,
          `/campaigns/${params.id}/entities?${query}`,
        ),
        { headers: { "cache-control": "private, no-store" } },
      );
    }
    return json(
      await sessionState(
        fetch,
        cookies,
        params.id,
        url.searchParams.get("session"),
      ),
      {
        headers: { "cache-control": "private, no-store" },
      },
    );
  } catch (error) {
    return json(
      { error: error.message || "Could not refresh the session." },
      { status: 400 },
    );
  }
}

export async function POST({ request, fetch, cookies, params }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  try {
    const input = await request.json();
    const base = `/campaigns/${params.id}/sessions`;
    let path = base;
    let method = "POST";
    let body = {};
    if (
      ["giveItem", "updateItem", "moveItem", "deleteItem"].includes(
        input.operation,
      )
    ) {
      path = `/campaigns/${params.id}/inventory`;
      if (input.operation !== "giveItem") {
        if (
          typeof input.itemId !== "string" ||
          !/^[0-9a-f-]{36}$/i.test(input.itemId)
        )
          throw new Error("Invalid item.");
        path += `/${input.itemId}`;
      }
      if (["giveItem", "moveItem"].includes(input.operation)) {
        if (
          typeof input.owner !== "string" ||
          (input.owner !== "party" && !/^[0-9a-f-]{36}$/i.test(input.owner))
        )
          throw new Error("Invalid owner.");
      }
      if (Object.hasOwn(input, "quantity") || input.operation === "giveItem") {
        const minimum = input.operation === "updateItem" ? 0 : 1;
        if (
          !Number.isInteger(input.quantity) ||
          input.quantity < minimum ||
          input.quantity > 1000000
        )
          throw new Error("Invalid quantity.");
      }
      if (input.operation === "giveItem") {
        body = {
          owner: input.owner,
          name: input.name,
          quantity: input.quantity,
          notes: input.notes || "",
          entity_id: input.entity_id || null,
          hidden_from_party: input.hidden_from_party === true,
          reason: input.reason || "",
          source_event_id: input.source_event_id || null,
        };
      } else if (input.operation === "updateItem") {
        method = "PATCH";
        for (const field of [
          "name",
          "quantity",
          "notes",
          "entity_id",
          "hidden_from_party",
          "reason",
        ])
          if (Object.hasOwn(input, field)) body[field] = input[field];
      } else if (input.operation === "moveItem") {
        path += "/move";
        body = { owner: input.owner, reason: input.reason || "" };
        if (Object.hasOwn(input, "quantity")) body.quantity = input.quantity;
      } else method = "DELETE";
    } else if (["note", "deleteNote"].includes(input.operation)) {
      path = `/campaigns/${params.id}/notes`;
      if (input.noteId) {
        if (!/^[0-9a-f-]{36}$/i.test(input.noteId))
          throw new Error("Invalid note.");
        path += `/${input.noteId}`;
        method = input.operation === "deleteNote" ? "DELETE" : "PATCH";
      } else if (input.operation === "deleteNote")
        throw new Error("Choose a note.");
      body = {
        kind: input.kind || "character",
        title: input.title || "",
        markdown: input.markdown || "",
        dm_readable: input.dmReadable === true,
        pinned: input.pinned === true,
      };
      if (method === "PATCH") delete body.kind;
      if (input.fromEventId) body.from_event_id = input.fromEventId;
    } else if (["reveal", "previewReveal"].includes(input.operation)) {
      path = `/campaigns/${params.id}/grants/${input.operation === "reveal" ? "bulk" : "preview"}`;
      body = {
        grants: input.pcIds.map((id) => ({
          entity_id: input.entityId,
          player_character_id: id,
          grant_scope: input.scope,
          revealed_details:
            input.scope === "partial"
              ? input.revealedDetails || {
                  clue: String(input.clue || "").trim(),
                }
              : null,
        })),
      };
    } else if (input.operation === "updateGrant") {
      if (!/^[0-9a-f-]{36}$/i.test(input.grantId))
        throw new Error("Invalid grant.");
      path = `/campaigns/${params.id}/grants/${input.grantId}`;
      method = "PATCH";
      body = {
        grant_scope: input.scope,
        revealed_details:
          input.scope === "partial" ? input.revealedDetails : {},
      };
    } else if (input.operation === "revoke") {
      if (!/^[0-9a-f-]{36}$/i.test(input.grantId))
        throw new Error("Invalid grant.");
      path = `/campaigns/${params.id}/grants/${input.grantId}?silent=${input.silent === true}`;
      method = "DELETE";
    } else if (input.operation !== "start") {
      if (!/^[0-9a-f-]{36}$/i.test(input.sessionId))
        throw new Error("Invalid session.");
      path += `/${input.sessionId}`;
      if (input.operation === "status") {
        method = "PATCH";
        body = { status: input.status };
      } else if (input.operation === "roll") {
        path += "/rolls";
        body = {
          formula: String(input.formula).replace(/^d/i, "1d"),
          label: input.label || "",
          visibility: input.visibility,
        };
      } else if (input.operation === "post") {
        path += "/events";
        const text = String(input.text || "").trim();
        if (!text || text.length > 8000)
          throw new Error("Write a message of up to 8,000 characters.");
        body = {
          kind: input.kind,
          audience: input.audience,
          audience_pc_ids: input.pcIds || [],
          body: { text },
          request_id: input.requestId || null,
        };
        if (input.kind === "narration" && input.replyTo) {
          body.body.reply_to = input.replyTo;
          body.body.resolved = input.resolved === true;
        }
      } else throw new Error("Unknown action.");
    }
    const result = await grimoireJson(fetch, cookies, path, {
      method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    return json(result);
  } catch (error) {
    return json(
      { error: error.message || "Could not save. Your draft is still here." },
      { status: 400 },
    );
  }
}
