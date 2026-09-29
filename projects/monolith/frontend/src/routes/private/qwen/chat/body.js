// The request FreeToken receives for a chat turn.

export const MODEL = "qwen3.6-27b";
export const DEFAULT_MAX_TOKENS = 8192;
export const MAX_MAX_TOKENS = 32768;

// Only the fields named here reach FreeToken; the body is never spread upstream.
export function upstreamBody(body) {
  const maxTokens =
    Number.isInteger(body.maxTokens) &&
    body.maxTokens > 0 &&
    body.maxTokens <= MAX_MAX_TOKENS
      ? body.maxTokens
      : DEFAULT_MAX_TOKENS;
  const messages = Array.isArray(body.messages)
    ? body.messages
        .filter(
          (m) =>
            (m?.role === "user" ||
              m?.role === "assistant" ||
              m?.role === "system") &&
            typeof m.content === "string",
        )
        .map((m) => ({ role: m.role, content: m.content }))
    : [];
  return {
    model: MODEL,
    messages,
    stream: true,
    // Usage (prompt, completion and cached tokens) arrives in the final chunk.
    stream_options: { include_usage: true },
    // No sampling fields: FreeToken applies the model's generation_config.
    max_tokens: maxTokens,
    chat_template_kwargs: { enable_thinking: body.enableThinking !== false },
  };
}

