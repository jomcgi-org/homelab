// The composer's "Send to" choice is a single select value: `table`, `dm`
// or `pc:<character id>`. Turn it into the message the session page posts
// to its state endpoint, which forwards the audience fields to the backend.
export function composeMessage({ text, dm, audience, replyTo, resolved }) {
  const toPc = audience.startsWith("pc:");
  return {
    operation: "post",
    text,
    kind: dm ? "narration" : "action",
    audience: toPc ? "pcs" : audience,
    pcIds: toPc ? [audience.slice(3)] : [],
    replyTo: replyTo?.id,
    resolved,
  };
}
