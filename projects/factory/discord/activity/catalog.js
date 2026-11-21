import { createElement } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";

// invariant: FACT-171
export function Poll({ components, counts, vote }) {
  return components.map((c) => {
    if (c?.component !== "Text" || typeof c.text !== "string") return null;
    if (c.id === "question" || c.id === "summary")
      return createElement(Text, { key: c.id }, c.text);
    if (typeof c.id !== "string" || !/^option(?:[1-9]|10)$/.test(c.id))
      return null;
    return createElement(
      Button,
      { key: c.id, onClick: () => vote(c.id) },
      c.text + " (" + (counts[c.id] ?? 0) + ")",
    );
  });
}
