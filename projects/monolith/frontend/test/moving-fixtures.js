// Invented records only. The browser clock is fixed to this same date.
export const clock = "2026-10-19T12:00:00Z";
export function movingState(empty = false) {
  const tasks = [
    {
      id: "task-pack",
      track: "admin",
      title: "Pack the kitchen and label the fragile boxes",
      note: "Keep the inventory with the boxes",
      owner: "both",
      due_on: "2026-10-18",
      value_cad: null,
      done_at: null,
    },
    {
      id: "task-sell",
      track: "sell",
      title: "Sell the dining table",
      note: "Collection from the front door",
      owner: "joe",
      due_on: "2026-10-22",
      value_cad: 125,
      done_at: null,
    },
    {
      id: "task-done",
      track: "ship",
      title: "Book the moving van",
      note: null,
      owner: "both",
      due_on: "2026-10-17",
      value_cad: null,
      done_at: clock,
    },
    {
      id: "task-anna",
      track: "people",
      title: "Confirm the arrival arrangements",
      note: "Call before arriving",
      owner: "anna",
      due_on: null,
      value_cad: null,
      done_at: null,
    },
  ].map((task) => ({ ...task, created_at: clock }));
  const spans = [
    {
      id: "span-move",
      kind: "move",
      label: "Crossing with boxes and the carefully packed kitchen",
      starts_on: "2026-11-01",
      ends_on: "2026-11-05",
      owner: "both",
    },
    {
      id: "span-trip",
      kind: "trip",
      label: "A long trip label alongside the crossing",
      starts_on: "2026-11-02",
      ends_on: "2026-11-12",
      owner: "anna",
    },
    {
      id: "span-work",
      kind: "work",
      label: "Final handover at the workshop",
      starts_on: "2026-10-15",
      ends_on: "2026-11-03",
      owner: "joe",
    },
    {
      id: "span-work-2",
      kind: "work",
      label: "Overlapping handover and documentation",
      starts_on: "2026-10-19",
      ends_on: "2026-11-09",
      owner: "both",
    },
    {
      id: "span-visit",
      kind: "visitor",
      label: "Family visit",
      starts_on: "2026-11-01",
      ends_on: "2026-11-08",
      owner: "both",
    },
  ];
  const milestones = Array.from({ length: 12 }, (_, i) => ({
    id: `milestone-${i}`,
    title: `Collection appointment ${i + 1} with a detailed inventory check`,
    occurs_on: `2026-11-${String(1 + Math.floor(i / 2)).padStart(2, "0")}`,
    owner: i % 2 ? "anna" : "both",
    gcal_state: ["held", "synced", "pending"][i % 3],
    gcal_event_id: i % 3 === 1 ? `synthetic-calendar-${i}` : null,
    gcal_synced_at: i % 3 === 1 ? clock : null,
  }));
  const roles = [
    {
      id: "role-1",
      company: "Example Workshop Cooperative",
      title: "Infrastructure engineer with a long descriptive title",
      owner: "joe",
      stage: "interview",
      next_on: "2026-10-25",
      span_id: "span-work",
    },
  ];
  const collisions = [
    {
      type: "span_span",
      item1_id: "span-move",
      item2_id: "span-trip",
      overlaps_from: "2026-11-02",
      overlaps_to: "2026-11-05",
      acked_by: null,
      ack_note: null,
    },
  ];
  return {
    viewer: "joe",
    tasks: empty ? [] : tasks,
    spans: empty ? [] : spans,
    milestones: empty ? [] : milestones,
    roles: empty ? [] : roles,
    collisions: empty ? [] : collisions,
    progress: empty ? 0 : 0.25,
  };
}

export const readyData = (overrides = {}) => ({
  status: "ready",
  mode: "",
  scope: "mine",
  state: movingState(),
  ...overrides,
});
