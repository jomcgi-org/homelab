/**
 * The page's Content-Security-Policy allows no inline style except the sheets
 * listed in public/style-hashes.json, which the backend reads from the built
 * bundle (grimoire/web.py). Astryx injects one fixed sheet the first time an
 * overlay opens. If an upgrade changes it, or a component starts injecting
 * another, this test prints the hashes to commit.
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { expect, it } from "vitest";
import { Button } from "@astryxdesign/core/Button";
import { Dialog } from "@astryxdesign/core/Dialog";
import { Selector } from "@astryxdesign/core/Selector";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { createQueryClient, Providers } from "./app/Providers";

const allowed: string[] = JSON.parse(
  readFileSync(
    join(import.meta.dirname, "../public/style-hashes.json"),
    "utf8",
  ),
);

function Overlays() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  return (
    <>
      <Tooltip content="Hint">
        <Button label="Open" onClick={() => setOpen(true)} />
      </Tooltip>
      <Selector
        label="Pick"
        options={["One", "Two"]}
        value={value}
        onChange={setValue}
      />
      <Dialog isOpen={open} onOpenChange={setOpen}>
        Body
      </Dialog>
    </>
  );
}

it("injects only the inline styles the policy allows", async () => {
  render(
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <Overlays />
      </Providers>
    </MemoryRouter>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Open" }));
  await user.click(screen.getByRole("combobox", { name: "Pick" }));
  const injected = [...document.querySelectorAll("style")].map(
    (style) =>
      `sha256-${createHash("sha256")
        .update(style.textContent ?? "")
        .digest("base64")}`,
  );
  expect(injected.length).toBeGreaterThan(0);
  expect([...new Set(injected)].sort()).toEqual([...allowed].sort());
});
