const ids = ["failure", "lifeboat", "conserve", "burn", "reentry", "landing"];
// Keep partial JSON out of the visual. Only the expected ordered events can appear.
export function rescueEvents(answer, complete = false) {
  const lines = answer.split("\n");
  if (!complete) lines.pop();
  const events = [];
  for (const line of lines) {
    try {
      const event = JSON.parse(line);
      if (
        event.id !== ids[events.length] ||
        typeof event.title !== "string" ||
        typeof event.detail !== "string"
      )
        continue;
      events.push({ id: event.id, title: event.title, detail: event.detail });
    } catch {
      /* The next token may complete this line. */
    }
  }
  return events;
}
