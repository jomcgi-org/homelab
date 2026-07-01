def extract_text(data: dict) -> str:
    """Return the assistant message text from a chat-completions API response."""
    return data["choices"][0]["message"].get("content", "")
