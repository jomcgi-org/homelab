const API_BASE = process.env.API_BASE;

export async function load({ fetch, url }) {
  const selectedProject = url.searchParams.get("project") ?? "";
  const selectedTechnology = url.searchParams.get("technology") ?? "";
  const requestedMonth = url.searchParams.get("month") ?? "";
  const endpoint = new URL(`${API_BASE}/api/updates`);
  if (selectedProject) endpoint.searchParams.set("project", selectedProject);
  if (selectedTechnology)
    endpoint.searchParams.set("technology", selectedTechnology);
  if (requestedMonth) endpoint.searchParams.set("month", requestedMonth);

  try {
    const response = await fetch(endpoint, {
      signal: AbortSignal.timeout(10000),
    });
    if (response.status === 422 && requestedMonth) {
      return {
        updates: [],
        months: [],
        projects: [],
        technologies: [],
        selectedMonth: requestedMonth,
        selectedProject,
        selectedTechnology,
        invalidMonth: true,
        error: false,
      };
    }
    if (!response.ok)
      throw new Error(`updates API returned ${response.status}`);
    const archive = await response.json();
    return {
      updates: archive.updates ?? [],
      months: archive.months ?? [],
      projects: archive.projects ?? [],
      technologies: archive.technologies ?? [],
      selectedMonth: archive.selected_month ?? requestedMonth,
      selectedProject,
      selectedTechnology,
      invalidMonth: false,
      error: false,
    };
  } catch {
    return {
      updates: [],
      months: [],
      projects: [],
      technologies: [],
      selectedMonth: requestedMonth,
      selectedProject,
      selectedTechnology,
      invalidMonth: false,
      error: true,
    };
  }
}
