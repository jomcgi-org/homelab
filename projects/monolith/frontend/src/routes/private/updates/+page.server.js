const API_BASE = process.env.API_BASE;

export async function load({ fetch, url }) {
  const selectedProject = url.searchParams.get("project") ?? "";
  const selectedTechnology = url.searchParams.get("technology") ?? "";
  const endpoint = new URL(`${API_BASE}/api/updates`);
  if (selectedProject) endpoint.searchParams.set("project", selectedProject);
  if (selectedTechnology)
    endpoint.searchParams.set("technology", selectedTechnology);

  try {
    const response = await fetch(endpoint, {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok)
      throw new Error(`updates API returned ${response.status}`);
    const archive = await response.json();
    return {
      updates: archive.updates ?? [],
      projects: archive.projects ?? [],
      technologies: archive.technologies ?? [],
      selectedProject,
      selectedTechnology,
      error: false,
    };
  } catch {
    return {
      updates: [],
      projects: [],
      technologies: [],
      selectedProject,
      selectedTechnology,
      error: true,
    };
  }
}
