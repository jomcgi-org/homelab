import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Regression guard for the Grimoire gate's admission path. TurnstileGate
// defaults `admit` to the notes-chat helper, which POSTs to /chat/session: a
// route that does not exist under this app, so a gate using the default never
// admits a visitor (every solve 404s and the widget reports an error). Every
// TurnstileGate under the Grimoire tree must wire an admit from the Grimoire's
// own admission module.
const grimoireDir = new URL(".", import.meta.url);
const layout = readFileSync(new URL("+layout.svelte", grimoireDir), "utf8");

describe("grimoire app shell gate", () => {
  it("admits through the grimoire chat session proxy, not the notes chat one", () => {
    expect(layout).toMatch(
      /import \{ createChatSession \} from "\$lib\/public\/grimoire\/chat\/admission\.js"/,
    );
    expect(layout).not.toMatch(/\$lib\/public\/chat\/admission\.js/);
    // The gate element passes the Grimoire helper explicitly rather than
    // relying on TurnstileGate's default.
    const gate = layout.match(/<TurnstileGate[\s\S]*?\/>/);
    expect(gate).not.toBeNull();
    expect(gate[0]).toMatch(/admit=\{createChatSession\}/);
  });
});
