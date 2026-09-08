// The three verification states are the record's whole vocabulary, and the
// page says them in several places: the sidebar filter, the (?) panel beside
// it, and the legend on the landing view. They lived as separate hardcoded
// sentences and drifted, so the sidebar and the legend ended up disagreeing
// about what "unverified" meant. Defining them once here makes that
// impossible: a wording change is one edit in one file.
//
// `state` matches the `verification_state` column, except for "contradicted",
// which is the reader-facing name for a fact carrying `disputed`. The label is
// deliberately not "disputed": the section listing these pairs is called
// Contradictions, and one word for one idea beats two.
export const MARK_DEFINITIONS = [
  {
    state: "verified",
    label: "Verified",
    definition: "trusted fact",
  },
  {
    state: "unverified",
    label: "Unverified",
    definition: "claim without enough evidence",
  },
  {
    state: "disputed",
    label: "Contradicted",
    definition: "reports that disagree with each other",
  },
];
