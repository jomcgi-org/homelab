import { Text } from "@astryxdesign/core/Text";
import "./notes.css";

/**
 * A note's body exactly as its author typed it. Players write these, so the
 * text is never interpreted as markup.
 */
export function NoteText({ text }: { text: string }) {
  if (!text) return null;
  return (
    <Text type="body" as="p" className="note-text">
      {text}
    </Text>
  );
}
