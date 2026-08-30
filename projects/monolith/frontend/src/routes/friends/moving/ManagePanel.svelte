<script>
  import { onDestroy } from "svelte";
  import {
    blankToNull,
    collisionWording,
    formatCad,
    formatShortDate,
    formatTaskDueDate,
    GCAL_STATE_LABELS,
    OWNER_LABELS,
    patchDiff,
    ROLE_STAGE_LABELS,
    SPAN_KIND_LABELS,
    TRACK_LABELS,
    titleCaseName,
  } from "./moving.js";

  let {
    spans = $bindable(),
    milestones = $bindable(),
    tasks = $bindable(),
    roles = $bindable(),
    collisions = $bindable(),
    viewer,
  } = $props();

  let editing = $state(null);
  let pendingIds = $state(new Set());
  let manageError = $state("");
  let undo = $state(null);
  let undoTimer;

  const collisionRows = $derived(
    collisions
      .map((collision) => ({
        ...collision,
        wording: collisionWording(collision, tasks, spans),
      }))
      .filter((collision) => collision.wording),
  );

  const spansForSelect = $derived(
    spans.filter((span) => !span.id.startsWith("pending-")),
  );

  const fields = {
    spans: ["kind", "label", "starts_on", "ends_on", "owner"],
    milestones: ["title", "occurs_on", "owner", "gcal_state"],
    tasks: ["track", "title", "note", "owner", "due_on", "value_cad"],
    roles: ["company", "title", "owner", "stage", "next_on", "span_id"],
  };

  onDestroy(() => clearTimeout(undoTimer));

  function listFor(type) {
    if (type === "spans") return spans;
    if (type === "milestones") return milestones;
    if (type === "tasks") return tasks;
    return roles;
  }

  function setList(type, value) {
    if (type === "spans") spans = value;
    else if (type === "milestones") milestones = value;
    else if (type === "tasks") tasks = value;
    else roles = value;
  }

  function pendingKey(type, id) {
    return `${type}:${id}`;
  }

  function setPending(key, pending) {
    const next = new Set(pendingIds);
    if (pending) next.add(key);
    else next.delete(key);
    pendingIds = next;
  }

  function canEdit(row) {
    return row.owner === viewer || row.owner === "both";
  }

  function ownerTitle(row) {
    return canEdit(row) ? undefined : `belongs to ${row.owner}`;
  }

  function clearUndo() {
    clearTimeout(undoTimer);
    undo = null;
  }

  function showUndo(type, item) {
    clearUndo();
    undo = { type, item, label: entityLabel(type, item) };
    undoTimer = setTimeout(() => {
      undo = null;
    }, 8_000);
  }

  function beginEdit(type, item) {
    clearUndo();
    manageError = "";
    editing = { type, id: item.id, original: item };
  }

  function cancelEdit() {
    editing = null;
  }

  function cancelOnEscape(event) {
    if (event.key === "Escape" && editing) {
      event.preventDefault();
      editing = null;
    }
  }

  function valuesFromForm(type, form) {
    const values = Object.fromEntries(new FormData(form));
    if (type === "tasks") {
      values.track = blankToNull(values.track);
      values.note = blankToNull(values.note);
      values.due_on = blankToNull(values.due_on);
      values.value_cad = blankToNull(values.value_cad);
    } else if (type === "roles") {
      values.stage = blankToNull(values.stage);
      values.next_on = blankToNull(values.next_on);
      values.span_id = blankToNull(values.span_id);
    }
    return values;
  }

  function optimisticShape(type, id, values) {
    if (type === "tasks") {
      return {
        id,
        done_at: null,
        created_at: new Date().toISOString(),
        ...values,
      };
    }
    if (type === "milestones") {
      return { id, gcal_event_id: null, gcal_synced_at: null, ...values };
    }
    return { id, ...values };
  }

  function createPayload(type, item) {
    return Object.fromEntries(
      fields[type].map((field) => [field, item[field]]),
    );
  }

  function editableValues(type, item) {
    const values = createPayload(type, item);
    if (type === "tasks" && values.value_cad != null) {
      values.value_cad = String(values.value_cad);
    }
    return values;
  }

  function entityLabel(type, item) {
    if (type === "spans") return item.label;
    if (type === "roles") return `${item.company}, ${item.title}`;
    return item.title;
  }

  async function responseMessage(response, fallback) {
    try {
      const body = await response.json();
      // FastAPI validation errors use an array of objects for detail.
      return typeof body.detail === "string"
        ? `${fallback}: ${body.detail}`
        : fallback;
    } catch {
      return fallback;
    }
  }

  async function createEntity(event, type) {
    event.preventDefault();
    clearUndo();
    const form = event.currentTarget;
    const values = valuesFromForm(type, form);
    const tempId = `pending-${crypto.randomUUID()}`;
    const optimistic = optimisticShape(type, tempId, values);
    const key = pendingKey(type, tempId);
    const before = listFor(type);

    manageError = "";
    setPending(key, true);
    setList(type, [...before, optimistic]);
    form.reset();
    if (form.elements.owner) form.elements.owner.value = "both";

    try {
      const response = await fetch(`/api/moving/${type}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!response.ok) {
        throw new Error(
          await responseMessage(response, `Could not add ${type.slice(0, -1)}`),
        );
      }
      const created = await response.json();
      setList(
        type,
        listFor(type).map((item) => (item.id === tempId ? created : item)),
      );
    } catch (error) {
      setList(
        type,
        listFor(type).filter((item) => item.id !== tempId),
      );
      manageError = error.message || `Could not add ${type.slice(0, -1)}.`;
    } finally {
      setPending(key, false);
    }
  }

  async function saveEntity(event, type, original) {
    event.preventDefault();
    clearUndo();
    const edited = valuesFromForm(type, event.currentTarget);
    const patch = patchDiff(
      editableValues(type, original),
      edited,
      fields[type],
    );
    if (Object.keys(patch).length === 0) {
      cancelEdit();
      return;
    }

    const key = pendingKey(type, original.id);
    manageError = "";
    setPending(key, true);
    setList(
      type,
      listFor(type).map((item) =>
        item.id === original.id ? { ...item, ...patch } : item,
      ),
    );
    editing = null;

    try {
      const response = await fetch(
        `/api/moving/${type}/${encodeURIComponent(original.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        },
      );
      if (!response.ok) {
        throw new Error(
          await responseMessage(
            response,
            `Could not update ${type.slice(0, -1)}`,
          ),
        );
      }
      const updated = await response.json();
      setList(
        type,
        listFor(type).map((item) => (item.id === original.id ? updated : item)),
      );
    } catch (error) {
      setList(
        type,
        listFor(type).map((item) =>
          item.id === original.id ? original : item,
        ),
      );
      manageError = error.message || `Could not update ${type.slice(0, -1)}.`;
    } finally {
      setPending(key, false);
    }
  }

  async function deleteEntity(type, item) {
    clearUndo();
    const before = listFor(type);
    const deletedIndex = before.findIndex((row) => row.id === item.id);
    const key = pendingKey(type, item.id);
    manageError = "";
    setPending(key, true);
    setList(
      type,
      before.filter((row) => row.id !== item.id),
    );

    try {
      const response = await fetch(
        `/api/moving/${type}/${encodeURIComponent(item.id)}`,
        { method: "DELETE" },
      );
      if (!response.ok) {
        throw new Error(
          await responseMessage(
            response,
            `Could not delete ${type.slice(0, -1)}`,
          ),
        );
      }
      if (editing?.id === item.id) editing = null;
      showUndo(type, item);
    } catch (error) {
      const current = listFor(type);
      const insertAt =
        deletedIndex < 0
          ? current.length
          : Math.min(deletedIndex, current.length);
      setList(type, [
        ...current.slice(0, insertAt),
        item,
        ...current.slice(insertAt),
      ]);
      manageError = error.message || `Could not delete ${type.slice(0, -1)}.`;
    } finally {
      setPending(key, false);
    }
  }

  async function restoreDeleted() {
    if (!undo) return;
    const deleted = undo;
    clearUndo();
    const values = createPayload(deleted.type, deleted.item);
    const tempId = `pending-${crypto.randomUUID()}`;
    const optimistic = optimisticShape(deleted.type, tempId, values);
    const key = pendingKey(deleted.type, tempId);

    manageError = "";
    setPending(key, true);
    setList(deleted.type, [...listFor(deleted.type), optimistic]);
    try {
      const response = await fetch(`/api/moving/${deleted.type}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!response.ok) {
        throw new Error(
          await responseMessage(
            response,
            `Could not restore ${deleted.type.slice(0, -1)}`,
          ),
        );
      }
      const created = await response.json();
      setList(
        deleted.type,
        listFor(deleted.type).map((item) =>
          item.id === tempId ? created : item,
        ),
      );
      if (deleted.type === "tasks" && deleted.item.done_at) {
        const doneKey = pendingKey("tasks", created.id);
        const beforeDone = created.done_at ?? null;
        setPending(doneKey, true);
        setList(
          "tasks",
          listFor("tasks").map((item) =>
            item.id === created.id
              ? { ...item, done_at: deleted.item.done_at }
              : item,
          ),
        );
        try {
          const doneResponse = await fetch(
            `/api/moving/tasks/${encodeURIComponent(created.id)}/done`,
            { method: "POST" },
          );
          if (!doneResponse.ok)
            throw new Error("task completion restore failed");
          const completed = await doneResponse.json();
          setList(
            "tasks",
            listFor("tasks").map((item) =>
              item.id === created.id ? completed : item,
            ),
          );
        } catch {
          setList(
            "tasks",
            listFor("tasks").map((item) =>
              item.id === created.id ? { ...item, done_at: beforeDone } : item,
            ),
          );
          manageError = "Failed to restore task completion state";
        } finally {
          setPending(doneKey, false);
        }
      }
    } catch (error) {
      setList(
        deleted.type,
        listFor(deleted.type).filter((item) => item.id !== tempId),
      );
      manageError =
        error.message || `Could not restore ${deleted.type.slice(0, -1)}.`;
    } finally {
      setPending(key, false);
    }
  }

  function collisionKey(collision) {
    return `${collision.item1_id}:${collision.item2_id}`;
  }

  function replaceCollision(target, replacement) {
    collisions = collisions.map((collision) =>
      collision.item1_id === target.item1_id &&
      collision.item2_id === target.item2_id
        ? replacement
        : collision,
    );
  }

  async function acknowledge(event, collision) {
    event.preventDefault();
    clearUndo();
    const note = String(
      new FormData(event.currentTarget).get("note") ?? "",
    ).trim();
    const body = note ? { note } : {};
    const key = `collision:${collisionKey(collision)}`;
    const optimistic = {
      ...collision,
      acked_by: viewer,
      ack_note: note || collision.ack_note || null,
    };

    manageError = "";
    setPending(key, true);
    replaceCollision(collision, optimistic);
    try {
      const response = await fetch(
        `/api/moving/collisions/${encodeURIComponent(collision.item1_id)}/${encodeURIComponent(collision.item2_id)}/ack`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok) {
        throw new Error(
          await responseMessage(response, "Could not acknowledge collision"),
        );
      }
      const ack = await response.json();
      replaceCollision(optimistic, {
        ...optimistic,
        acked_by: ack.acked_by,
        ack_note: ack.note,
      });
    } catch (error) {
      replaceCollision(optimistic, collision);
      manageError = error.message || "Could not acknowledge collision.";
    } finally {
      setPending(key, false);
    }
  }

  async function unacknowledge(collision) {
    clearUndo();
    const key = `collision:${collisionKey(collision)}`;
    const optimistic = { ...collision, acked_by: null, ack_note: null };

    manageError = "";
    setPending(key, true);
    replaceCollision(collision, optimistic);
    try {
      const response = await fetch(
        `/api/moving/collisions/${encodeURIComponent(collision.item1_id)}/${encodeURIComponent(collision.item2_id)}/ack`,
        { method: "DELETE" },
      );
      if (!response.ok) {
        throw new Error(
          await responseMessage(response, "Could not remove acknowledgement"),
        );
      }
    } catch (error) {
      replaceCollision(optimistic, collision);
      manageError = error.message || "Could not remove acknowledgement.";
    } finally {
      setPending(key, false);
    }
  }

  function spanLabel(spanId) {
    return spans.find((span) => span.id === spanId)?.label ?? "No span";
  }
</script>

<svelte:window onkeydown={cancelOnEscape} />

<div class="manage">
  {#if manageError}
    <p class="manage-error" role="alert">{manageError}</p>
  {/if}

  {#if undo}
    <div class="undo-strip" role="status">
      <span>Deleted '{undo.label}'.</span>
      <button type="button" onclick={restoreDeleted}>Undo</button>
    </div>
  {/if}

  <section class="tsec manage-section spans" aria-labelledby="manage-spans">
    <div class="th" id="manage-spans">Spans <span>{spans.length}</span></div>
    <div class="manage-table">
      <div class="manage-row column-head">
        <span>Kind</span><span>Label</span><span>Starts</span><span>Ends</span
        ><span>Owner</span><span>Actions</span>
      </div>
      {#each spans as span (span.id)}
        {#if editing?.type === "spans" && editing.id === span.id}
          <form
            class="manage-row edit-row"
            onsubmit={(event) => saveEntity(event, "spans", editing.original)}
          >
            <label
              ><span class="sr-only">Kind</span><select
                name="kind"
                value={span.kind}
                required
                >{#each Object.entries(SPAN_KIND_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            >
            <label
              ><span class="sr-only">Label</span><input
                name="label"
                type="text"
                value={span.label}
                required
              /></label
            >
            <label
              ><span class="sr-only">Starts on</span><input
                name="starts_on"
                type="date"
                value={span.starts_on}
                required
              /></label
            >
            <label
              ><span class="sr-only">Ends on</span><input
                name="ends_on"
                type="date"
                value={span.ends_on}
                required
              /></label
            >
            <label
              ><span class="sr-only">Owner</span><select
                name="owner"
                value={span.owner}
                required
                >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            >
            <span class="row-actions"
              ><button class="save" type="submit">Save</button><button
                type="button"
                onclick={cancelEdit}>Cancel</button
              ></span
            >
          </form>
        {:else}
          <div class="manage-row">
            <span
              ><i class={`sw hue-${span.kind}`} aria-hidden="true"
              ></i>{SPAN_KIND_LABELS[span.kind] ?? span.kind}</span
            >
            <strong>{span.label}</strong><span class="mono"
              >{formatShortDate(span.starts_on)}</span
            ><span class="mono">{formatShortDate(span.ends_on)}</span><span
              >{titleCaseName(span.owner)}</span
            >
            <span class="row-actions"
              ><button
                type="button"
                disabled={!canEdit(span) ||
                  pendingIds.has(pendingKey("spans", span.id))}
                title={ownerTitle(span)}
                onclick={() => beginEdit("spans", span)}>Edit</button
              ><button
                class="danger"
                type="button"
                disabled={!canEdit(span) ||
                  pendingIds.has(pendingKey("spans", span.id))}
                title={ownerTitle(span)}
                onclick={() => deleteEntity("spans", span)}>Delete</button
              ></span
            >
          </div>
        {/if}
      {/each}
      <form
        class="manage-row add-row"
        aria-label="Add span"
        onsubmit={(event) => createEntity(event, "spans")}
      >
        <label
          ><span class="sr-only">Kind</span><select name="kind" required
            >{#each Object.entries(SPAN_KIND_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        >
        <label
          ><span class="sr-only">Label</span><input
            name="label"
            type="text"
            placeholder="New span"
            required
          /></label
        >
        <label
          ><span class="sr-only">Starts on</span><input
            name="starts_on"
            type="date"
            required
          /></label
        >
        <label
          ><span class="sr-only">Ends on</span><input
            name="ends_on"
            type="date"
            required
          /></label
        >
        <label
          ><span class="sr-only">Owner</span><select
            name="owner"
            value="both"
            required
            >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        >
        <span class="row-actions"
          ><button class="add" type="submit">Add</button></span
        >
      </form>
    </div>
  </section>

  <section
    class="tsec manage-section milestones"
    aria-labelledby="manage-milestones"
  >
    <div class="th" id="manage-milestones">
      Milestones <span>{milestones.length}</span>
    </div>
    <div class="manage-table">
      <div class="manage-row column-head">
        <span>Title</span><span>Date</span><span>Owner</span><span
          >Calendar</span
        ><span>Sync</span><span>Actions</span>
      </div>
      {#each milestones as milestone (milestone.id)}
        {#if editing?.type === "milestones" && editing.id === milestone.id}
          <form
            class="manage-row edit-row"
            onsubmit={(event) =>
              saveEntity(event, "milestones", editing.original)}
          >
            <label
              ><span class="sr-only">Title</span><input
                name="title"
                type="text"
                value={milestone.title}
                required
              /></label
            >
            <label
              ><span class="sr-only">Occurs on</span><input
                name="occurs_on"
                type="date"
                value={milestone.occurs_on}
                required
              /></label
            >
            <label
              ><span class="sr-only">Owner</span><select
                name="owner"
                value={milestone.owner}
                required
                >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            >
            <label
              ><span class="sr-only">Calendar state</span><select
                name="gcal_state"
                value={milestone.gcal_state}
                required
                >{#each Object.entries(GCAL_STATE_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            >
            <span class="sync-state"
              >{milestone.gcal_synced_at
                ? `synced ${formatShortDate(milestone.gcal_synced_at)}`
                : "not synced"}{#if milestone.gcal_event_id}<small
                  title={milestone.gcal_event_id}
                  >{milestone.gcal_event_id}</small
                >{/if}</span
            >
            <span class="row-actions"
              ><button class="save" type="submit">Save</button><button
                type="button"
                onclick={cancelEdit}>Cancel</button
              ></span
            >
          </form>
        {:else}
          <div class="manage-row">
            <strong>{milestone.title}</strong><span class="mono"
              >{formatShortDate(milestone.occurs_on)}</span
            ><span>{titleCaseName(milestone.owner)}</span><span
              >{GCAL_STATE_LABELS[milestone.gcal_state] ??
                milestone.gcal_state}</span
            ><span class="sync-state"
              >{milestone.gcal_synced_at
                ? `synced ${formatShortDate(milestone.gcal_synced_at)}`
                : "not synced"}{#if milestone.gcal_event_id}<small
                  title={milestone.gcal_event_id}
                  >{milestone.gcal_event_id}</small
                >{/if}</span
            ><span class="row-actions"
              ><button
                type="button"
                disabled={!canEdit(milestone) ||
                  pendingIds.has(pendingKey("milestones", milestone.id))}
                title={ownerTitle(milestone)}
                onclick={() => beginEdit("milestones", milestone)}>Edit</button
              ><button
                class="danger"
                type="button"
                disabled={!canEdit(milestone) ||
                  pendingIds.has(pendingKey("milestones", milestone.id))}
                title={ownerTitle(milestone)}
                onclick={() => deleteEntity("milestones", milestone)}
                >Delete</button
              ></span
            >
          </div>
        {/if}
      {/each}
      <form
        class="manage-row add-row"
        aria-label="Add milestone"
        onsubmit={(event) => createEntity(event, "milestones")}
      >
        <label
          ><span class="sr-only">Title</span><input
            name="title"
            type="text"
            placeholder="New milestone"
            required
          /></label
        ><label
          ><span class="sr-only">Occurs on</span><input
            name="occurs_on"
            type="date"
            required
          /></label
        ><label
          ><span class="sr-only">Owner</span><select
            name="owner"
            value="both"
            required
            >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><label
          ><span class="sr-only">Calendar state</span><select
            name="gcal_state"
            value="queued"
            required
            >{#each Object.entries(GCAL_STATE_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><span class="sync-state">not synced</span><span class="row-actions"
          ><button class="add" type="submit">Add</button></span
        >
      </form>
    </div>
  </section>

  <section
    class="tsec manage-section tasks-manage"
    aria-labelledby="manage-tasks"
  >
    <div class="th" id="manage-tasks">Tasks <span>{tasks.length}</span></div>
    <div class="manage-table">
      <div class="manage-row column-head">
        <span>Track</span><span>Task</span><span>Note</span><span>Owner</span
        ><span>Due</span><span>Value</span><span>State</span><span>Actions</span
        >
      </div>
      {#each tasks as task (task.id)}
        {#if editing?.type === "tasks" && editing.id === task.id}
          <form
            class="manage-row edit-row"
            onsubmit={(event) => saveEntity(event, "tasks", editing.original)}
          >
            <label
              ><span class="sr-only">Track</span><select
                name="track"
                value={task.track ?? ""}
                ><option value="">No track</option
                >{#each Object.entries(TRACK_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            ><label
              ><span class="sr-only">Title</span><input
                name="title"
                type="text"
                value={task.title}
                required
              /></label
            ><label
              ><span class="sr-only">Note</span><input
                name="note"
                type="text"
                value={task.note ?? ""}
              /></label
            ><label
              ><span class="sr-only">Owner</span><select
                name="owner"
                value={task.owner}
                required
                >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            ><label
              ><span class="sr-only">Due on</span><input
                name="due_on"
                type="date"
                value={task.due_on ?? ""}
              /></label
            ><label
              ><span class="sr-only">Value in Canadian dollars</span><input
                name="value_cad"
                type="number"
                min="0"
                step="0.01"
                value={task.value_cad ?? ""}
              /></label
            ><span class:done-state={task.done_at} class="task-state"
              >{task.done_at ? "Done" : "Open"}</span
            ><span class="row-actions"
              ><button class="save" type="submit">Save</button><button
                type="button"
                onclick={cancelEdit}>Cancel</button
              ></span
            >
          </form>
        {:else}
          <div class="manage-row">
            <span
              ><i class={`sw hue-${task.track ?? "none"}`} aria-hidden="true"
              ></i>{task.track ? TRACK_LABELS[task.track] : "No track"}</span
            ><strong>{task.title}</strong><span>{task.note || "No note"}</span
            ><span>{titleCaseName(task.owner)}</span><span class="mono"
              >{formatTaskDueDate(task.due_on)}</span
            ><span class="mono"
              >{task.value_cad == null
                ? "No value"
                : formatCad(task.value_cad)}</span
            ><span class:done-state={task.done_at} class="task-state"
              >{task.done_at ? "Done" : "Open"}</span
            ><span class="row-actions"
              ><button
                type="button"
                disabled={!canEdit(task) ||
                  pendingIds.has(pendingKey("tasks", task.id))}
                title={ownerTitle(task)}
                onclick={() => beginEdit("tasks", task)}>Edit</button
              ><button
                class="danger"
                type="button"
                disabled={!canEdit(task) ||
                  pendingIds.has(pendingKey("tasks", task.id))}
                title={ownerTitle(task)}
                onclick={() => deleteEntity("tasks", task)}>Delete</button
              ></span
            >
          </div>
        {/if}
      {/each}
      <form
        class="manage-row add-row"
        aria-label="Add task"
        onsubmit={(event) => createEntity(event, "tasks")}
      >
        <label
          ><span class="sr-only">Track</span><select name="track"
            ><option value="">No track</option
            >{#each Object.entries(TRACK_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><label
          ><span class="sr-only">Title</span><input
            name="title"
            type="text"
            placeholder="New task"
            required
          /></label
        ><label
          ><span class="sr-only">Note</span><input
            name="note"
            type="text"
            placeholder="Optional note"
          /></label
        ><label
          ><span class="sr-only">Owner</span><select
            name="owner"
            value="both"
            required
            >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><label
          ><span class="sr-only">Due on</span><input
            name="due_on"
            type="date"
          /></label
        ><label
          ><span class="sr-only">Value in Canadian dollars</span><input
            name="value_cad"
            type="number"
            min="0"
            step="0.01"
          /></label
        ><span class="task-state">Open</span><span class="row-actions"
          ><button class="add" type="submit">Add</button></span
        >
      </form>
    </div>
  </section>

  <section
    class="tsec manage-section roles-manage"
    aria-labelledby="manage-roles"
  >
    <div class="th" id="manage-roles">Roles <span>{roles.length}</span></div>
    <div class="manage-table">
      <div class="manage-row column-head">
        <span>Company</span><span>Title</span><span>Owner</span><span
          >Stage</span
        ><span>Next</span><span>Span</span><span>Actions</span>
      </div>
      {#each roles as role (role.id)}
        {#if editing?.type === "roles" && editing.id === role.id}
          <form
            class="manage-row edit-row"
            onsubmit={(event) => saveEntity(event, "roles", editing.original)}
          >
            <label
              ><span class="sr-only">Company</span><input
                name="company"
                type="text"
                value={role.company}
                required
              /></label
            ><label
              ><span class="sr-only">Title</span><input
                name="title"
                type="text"
                value={role.title}
                required
              /></label
            ><label
              ><span class="sr-only">Owner</span><select
                name="owner"
                value={role.owner}
                required
                >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            ><label
              ><span class="sr-only">Stage</span><select
                name="stage"
                value={role.stage ?? ""}
                ><option value="">No stage</option
                >{#each Object.entries(ROLE_STAGE_LABELS) as [value, label]}<option
                    {value}>{label}</option
                  >{/each}</select
              ></label
            ><label
              ><span class="sr-only">Next date</span><input
                name="next_on"
                type="date"
                value={role.next_on ?? ""}
              /></label
            ><label
              ><span class="sr-only">Linked span</span><select
                name="span_id"
                value={role.span_id ?? ""}
                ><option value="">No span</option
                >{#each spansForSelect as span (span.id)}<option value={span.id}
                    >{span.label}</option
                  >{/each}</select
              ></label
            ><span class="row-actions"
              ><button class="save" type="submit">Save</button><button
                type="button"
                onclick={cancelEdit}>Cancel</button
              ></span
            >
          </form>
        {:else}
          <div class="manage-row">
            <strong>{role.company}</strong><span>{role.title}</span><span
              >{titleCaseName(role.owner)}</span
            ><span
              >{role.stage ? ROLE_STAGE_LABELS[role.stage] : "No stage"}</span
            ><span class="mono"
              >{role.next_on ? formatShortDate(role.next_on) : "No date"}</span
            ><span>{spanLabel(role.span_id)}</span><span class="row-actions"
              ><button
                type="button"
                disabled={!canEdit(role) ||
                  pendingIds.has(pendingKey("roles", role.id))}
                title={ownerTitle(role)}
                onclick={() => beginEdit("roles", role)}>Edit</button
              ><button
                class="danger"
                type="button"
                disabled={!canEdit(role) ||
                  pendingIds.has(pendingKey("roles", role.id))}
                title={ownerTitle(role)}
                onclick={() => deleteEntity("roles", role)}>Delete</button
              ></span
            >
          </div>
        {/if}
      {/each}
      <form
        class="manage-row add-row"
        aria-label="Add role"
        onsubmit={(event) => createEntity(event, "roles")}
      >
        <label
          ><span class="sr-only">Company</span><input
            name="company"
            type="text"
            placeholder="Company"
            required
          /></label
        ><label
          ><span class="sr-only">Title</span><input
            name="title"
            type="text"
            placeholder="Role title"
            required
          /></label
        ><label
          ><span class="sr-only">Owner</span><select
            name="owner"
            value="both"
            required
            >{#each Object.entries(OWNER_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><label
          ><span class="sr-only">Stage</span><select name="stage"
            ><option value="">No stage</option
            >{#each Object.entries(ROLE_STAGE_LABELS) as [value, label]}<option
                {value}>{label}</option
              >{/each}</select
          ></label
        ><label
          ><span class="sr-only">Next date</span><input
            name="next_on"
            type="date"
          /></label
        ><label
          ><span class="sr-only">Linked span</span><select name="span_id"
            ><option value="">No span</option
            >{#each spansForSelect as span (span.id)}<option value={span.id}
                >{span.label}</option
              >{/each}</select
          ></label
        ><span class="row-actions"
          ><button class="add" type="submit">Add</button></span
        >
      </form>
    </div>
  </section>

  <section
    class="tsec manage-section collisions-manage"
    aria-labelledby="manage-collisions"
  >
    <div class="th" id="manage-collisions">
      Collisions <span>{collisionRows.length}</span>
    </div>
    <div class="collision-list">
      {#if collisionRows.length === 0}
        <p class="manage-empty">No date collisions.</p>
      {:else}
        {#each collisionRows as collision (`${collision.item1_id}:${collision.item2_id}`)}
          <div class:acked={collision.acked_by} class="collision-manage-row">
            <span class="collision-mark" aria-hidden="true"
              >{collision.type === "task_span" ? "▲" : "△"}</span
            >
            <span class="mono">{formatShortDate(collision.overlaps_from)}</span>
            <strong>{collision.wording}</strong>
            {#if collision.acked_by}
              <span class="ack-copy"
                >acked by {titleCaseName(collision.acked_by)}{collision.ack_note
                  ? `: ${collision.ack_note}`
                  : ""}</span
              >
              <button
                type="button"
                disabled={pendingIds.has(
                  `collision:${collisionKey(collision)}`,
                )}
                onclick={() => unacknowledge(collision)}>Unacknowledge</button
              >
            {:else}
              <form
                class="ack-form"
                onsubmit={(event) => acknowledge(event, collision)}
              >
                <label
                  ><span class="sr-only">Acknowledgement note</span><input
                    name="note"
                    type="text"
                    placeholder="Optional note"
                  /></label
                >
                <button
                  type="submit"
                  disabled={pendingIds.has(
                    `collision:${collisionKey(collision)}`,
                  )}>Acknowledge</button
                >
              </form>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
  </section>
</div>
