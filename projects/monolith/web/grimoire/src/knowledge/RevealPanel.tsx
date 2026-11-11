import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Drawer, DrawerHeader } from "@astryxdesign/core/Drawer";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { RevealEditor, type RevealCharacter } from "./RevealEditor";

export interface RevealPanelProps {
  campaignId: string;
  characters: RevealCharacter[];
}

const TITLE = "Reveal to your players";

/** The DM's control for revealing an entity to chosen characters. */
export function RevealPanel({ campaignId, characters }: RevealPanelProps) {
  const [open, setOpen] = useState(false);
  // The editor mounts on first open and then stays, so a search and a chosen
  // entity survive closing the drawer between reveals.
  const [started, setStarted] = useState(false);
  return (
    <>
      <Button
        label="Reveal knowledge"
        onClick={() => {
          setStarted(true);
          setOpen(true);
        }}
      />
      <Drawer
        isOpen={open}
        onOpenChange={setOpen}
        label={TITLE}
        purpose="form"
        width={480}
      >
        <Layout
          header={<DrawerHeader title={TITLE} onOpenChange={setOpen} />}
          content={
            <LayoutContent>
              {started && (
                <RevealEditor
                  key={campaignId}
                  campaignId={campaignId}
                  characters={characters}
                />
              )}
            </LayoutContent>
          }
        />
      </Drawer>
    </>
  );
}
