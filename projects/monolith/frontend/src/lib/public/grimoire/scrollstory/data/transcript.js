// Chat transcript for the scroll story's final "Ask the Grimoire" scene.
// This is a REAL answer captured from the live public Grimoire chat on
// 2026-07-06 (not a mock). The export shape is kept stable for API stability;
// isPlaceholder stays exported and is now false.
export const isPlaceholder = false;

export const transcript = {
  question: "Who is the Black Spider, and what does he actually want?",
  // Two paragraphs joined with a blank line; the renderer turns \n\n into a
  // paragraph break. Typographic apostrophes are as captured from the UI.
  answer:
    "The Black Spider is Nezznar, a cunning drow wizard who travels in black leather armor and robes while clutching a black staff topped with a carved spider. He's openly pulling the strings behind the Cragmaw goblins and the Redbrands to keep Wave Echo Cave his tightly guarded secret.\n\nHis actual goal is to reach the Forge of Spells inside that cave. He doesn't truly want your cooperation, even if he'll offer false surrender or promise to help clear out other monsters to keep you moving toward the entrance. He's deeply curious about your identities, allegiances, and objectives, and he files every answer away for potential future use. Don't let his chatter fool you: he intends to kill you the moment you outlive whatever usefulness you have to his plan.",
  // Subset of the real GROUNDED IN chips that map to page-49 entity names, so
  // the node-pulse matcher hits a graph node for each.
  groundedIn: ["Nezznar", "Black Spider", "Spider Staff", "Wave Echo Cave"],
  // Full real GROUNDED IN chip list for provenance (captured 2026-07-06):
  //   Spider (npc), Spider (creature), Spiders (table),
  //   Staff of the Black Spider (item), Nezznar the Black Spider (npc),
  //   Nezznar (npc), Tome Of Beasts 2: DEATHWEAVER, Vyerith (npc),
  //   Lost Mine Of Phandelver: PART 4: WAVE ECHO CAVE/ROLEPLAYING NEZZNAR
  //   (chunk).
};
