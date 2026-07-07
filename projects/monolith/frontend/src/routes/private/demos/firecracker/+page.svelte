<script>
  // /demos/firecracker (private tier): a tabbed launcher for the three
  // firecracker-backed projects (Python sandbox, Semgrep diff scan, Goose
  // agent). Each card opens a rich modal with a shared RunPanel that runs
  // a real invocation against the /api/demos/firecracker/* endpoints
  // (Task 6), shows live latency, and polls the real SigNoz trace for
  // that invocation into a waterfall.
  import ProjectModal from "$lib/private/components/demos/ProjectModal.svelte";
  import RunPanel from "$lib/private/components/demos/RunPanel.svelte";

  const PYTHON_SAMPLE = `import math

def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

print(f"fib(20) = {fib(20)}")
print(f"sqrt(2) = {math.sqrt(2):.6f}")
`;

  const SEMGREP_SAMPLE = `import subprocess


def run_backup(filename):
    # Builds a shell command from an unsanitised argument -- a classic
    # command-injection pattern that semgrep's python.lang.security
    # rules flag.
    cmd = f"tar czf backup.tar.gz {filename}"
    subprocess.call(cmd, shell=True)


def load_config(raw):
    # eval() on untrusted input.
    return eval(raw)
`;

  const GOOSE_TASK_SAMPLE =
    "Summarize what this repository's Firecracker demo page does, in three bullet points.";

  const PROJECTS = [
    {
      key: "python",
      label: "Python Sandbox",
      tagline: "Runs arbitrary Python inside a Firecracker microVM.",
      accent: "var(--yellow)",
      sample: { code: PYTHON_SAMPLE },
    },
    {
      key: "semgrep",
      label: "Semgrep Diff Scan",
      tagline: "Static-analysis scan of a file, run in the sandbox.",
      accent: "var(--coral)",
      sample: { path: "app/backup.py", code: SEMGREP_SAMPLE },
    },
    {
      key: "goose",
      label: "Goose Agent",
      tagline: "Kicks off an async agent task and polls it to completion.",
      accent: "var(--green)",
      sample: { task: GOOSE_TASK_SAMPLE },
    },
  ];

  let openProject = $state(null);

  function openModal(project) {
    openProject = project;
  }

  function closeModal() {
    openProject = null;
  }
</script>

<svelte:head><title>Firecracker demos · private.jomcgi.dev</title></svelte:head>

<section class="demos">
  <header class="demos-header">
    <h1 class="demos-title">Firecracker demos</h1>
    <p class="demos-lede">
      Three projects run for real inside Firecracker microVMs. Pick one,
      run it, and watch the live latency and the actual SigNoz trace come
      back for that invocation.
    </p>
  </header>

  <div class="cards">
    {#each PROJECTS as project (project.key)}
      <button
        type="button"
        class="card"
        style={`--accent-swatch: ${project.accent}`}
        onclick={() => openModal(project)}
      >
        <span class="card-swatch" aria-hidden="true"></span>
        <h2 class="card-title">{project.label}</h2>
        <p class="card-tagline">{project.tagline}</p>
        <span class="card-cta">Open &rarr;</span>
      </button>
    {/each}
  </div>
</section>

<ProjectModal project={openProject} onClose={closeModal}>
  {#if openProject}
    <RunPanel project={openProject} />
  {/if}
</ProjectModal>

<style>
  .demos {
    padding: 1.5rem 2.5rem 2.5rem;
    font-family: var(--font-mono);
    color: var(--fg);
    background: var(--bg);
    min-height: calc(100vh - 4rem);
  }

  .demos-header {
    max-width: 46rem;
    margin-bottom: 2rem;
  }

  .demos-title {
    font-size: 1.6rem;
    font-weight: 700;
    letter-spacing: -0.01em;
    margin: 0 0 0.5rem 0;
  }

  .demos-lede {
    font-size: 0.9rem;
    line-height: 1.6;
    color: var(--fg-secondary);
    margin: 0;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
    gap: 1.25rem;
    max-width: 60rem;
  }

  .card {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
    text-align: left;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-heavy);
    padding: 1.25rem 1.4rem;
    cursor: pointer;
    font-family: var(--font-mono);
    transform: translate(0, 0);
    transition:
      transform 0.1s ease,
      box-shadow 0.1s ease;
  }

  .card:hover,
  .card:focus-visible {
    transform: translate(-3px, -3px);
    box-shadow: 6px 6px 0 0 var(--fg);
  }

  .card:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .card-swatch {
    width: 1.4rem;
    height: 0.4rem;
    background: var(--accent-swatch, var(--accent));
    border: 1px solid var(--fg);
  }

  .card-title {
    font-size: 1.05rem;
    font-weight: 700;
    margin: 0;
  }

  .card-tagline {
    font-size: 0.8rem;
    color: var(--fg-secondary);
    line-height: 1.5;
    margin: 0;
  }

  .card-cta {
    margin-top: auto;
    padding-top: 0.5rem;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
  }

  @media (max-width: 640px) {
    .demos {
      padding: 1.25rem 1.25rem 2rem;
    }
  }
</style>
