// Invented listings, shared by the actual-page DOM tests and browser fixture.
// Dates are fixed so New and closing badges exercise the same branches in CI.
export const now = "2026-10-19T12:00:00Z";
export const longTown =
  "Inverness and the surrounding Highland community hospitals including Fort Augustus";
export const longReference =
  "NHS-HIGHLAND-ANAESTHETICS-CONSULTANT-2026-000123456789";
export const jobs = [
  {
    job_id: "today",
    reference: "NHS-101",
    title: "NHS-101 - Consultant Anaesthetist",
    town: "Glasgow",
    employment_type: "Permanent",
    salary_band: "Consultant (£111,430 - £148,064)",
    closing_date: "2026-10-19",
    first_seen_at: "2026-10-18T12:00:00Z",
    is_live: true,
  },
  {
    job_id: "tomorrow",
    title: "Locum Consultant in Anaesthetics",
    town: "Glasgow",
    closing_date: "2026-10-20",
    first_seen_at: "2026-10-12T12:00:00Z",
    is_live: true,
  },
  {
    job_id: "long",
    reference: longReference,
    title: `${longReference} - Consultant Anaesthetist with an interest in perioperative medicine and complex multidisciplinary care across the regional hospital network`,
    town: longTown,
    employment_type: "Permanent, full time or part time",
    salary_band: "Consultant (£111,430 - £148,064)",
    closing_date: "2026-10-26",
    first_seen_at: "2026-10-12T12:00:01Z",
    is_live: true,
  },
  {
    job_id: "later",
    reference: "REFONLY",
    title: "REFONLY",
    town: "Aberdeen",
    closing_date: "2026-10-27",
    first_seen_at: "invalid",
    is_live: true,
  },
  {
    job_id: "optional",
    title:
      "ConsultantAnaestheticsAndCriticalCareRegionalCoverWithoutWordBreaks",
    town: null,
    reference: null,
    employment_type: null,
    salary_band: null,
    closing_date: null,
    is_live: true,
  },
  {
    job_id: "closed",
    reference: "NHS-099",
    title: "NHS-099 - Consultant in Anaesthetics and Intensive Care Medicine",
    town: "Edinburgh",
    employment_type: "Permanent",
    salary_band: "Consultant",
    closing_date: "2026-10-18",
    first_seen_at: "2026-10-18T12:00:00Z",
    is_live: false,
  },
  {
    job_id: "past-long",
    reference: longReference,
    title: `${longReference} - Previous locum consultant anaesthetics vacancy supporting regional and remote hospital services`,
    town: longTown,
    closing_date: "2026-10-01",
    is_live: false,
  },
  { job_id: "past-optional", title: "Past consultant vacancy", is_live: false },
].map((job) => ({
  ...job,
  url: `https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId=fixture-${job.job_id}`,
}));

export function fixtureData(scenario = "populated") {
  return {
    hasBlog: true,
    listings: {
      jobs: jobs.filter((job) =>
        scenario === "empty-live"
          ? !job.is_live
          : scenario === "empty-history"
            ? job.is_live
            : true,
      ),
    },
  };
}
