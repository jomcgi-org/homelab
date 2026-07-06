// Hand-authored chat transcript for the scroll story's final "Ask the Grimoire"
// scene. This is a PLACEHOLDER written to read like a grounded sage answer over
// the baked page 49 entities (Nezznar / Wave Echo Cave / the villain reveal).
//
// TODO before merge: replace `answer` and `groundedIn` with a captured REAL
// sage answer to `question`, taken from the live public chat backend, and keep
// `groundedIn` to the entity names that answer actually cites (they must match
// entities on the baked page so the graph can pulse the matching nodes). Flip
// `isPlaceholder` to false once it is a real capture.
export const isPlaceholder = true;

export const transcript = {
  question: "Who is the Black Spider, and what does he actually want?",
  answer:
    "The Black Spider is Nezznar, a drow directing events from the shadows: he sent the Cragmaw goblins after Gundren Rockseeker and planted the Redbrands in Phandalin. He is exploring Wave Echo Cave with his giant spiders, hunting the legendary Forge of Spells. Capture him alive and deliver him to Sildar Hallwinter in Phandalin, and the party earns double XP for the quarry.",
  // Entity names the answer cites; each must resolve to a baked page entity so
  // its graph node can pulse as the citation chip appears.
  groundedIn: ["Nezznar", "Wave Echo Cave", "Redbrands", "Sildar Hallwinter"],
};
