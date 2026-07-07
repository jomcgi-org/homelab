<script>
  // /demos/firecracker (private tier): a full-page, Grimoire-style tabbed
  // tool for the three firecracker-backed projects (Python sandbox, Semgrep
  // diff scan, Goose agent). Each tab renders its own RunPanel full-page,
  // no modal: a modal close used to break mid-run because the dialog kept
  // its own escape/backdrop-click lifecycle independent of an in-flight
  // request, so we dropped the modal entirely in favor of a plain tabbed
  // page (mirrors the Grimoire app topbar in
  // src/routes/public/app/grimoire/+layout.svelte).
  import RunPanel from "$lib/private/components/demos/RunPanel.svelte";
  import "$lib/private/demos/theme.css";

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
    # Builds a shell command from an unsanitised argument, a classic
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
      label: "Sandbox",
      tagline: "Runs arbitrary Python inside a Firecracker microVM.",
      sample: { code: PYTHON_SAMPLE },
    },
    {
      key: "semgrep",
      label: "Semgrep",
      tagline: "Static-analysis scan of a file, run in the sandbox.",
      sample: { path: "app/backup.py", code: SEMGREP_SAMPLE },
    },
    {
      key: "goose",
      label: "Goose",
      tagline: "Kicks off an async agent task and polls it to completion.",
      sample: { task: GOOSE_TASK_SAMPLE },
    },
  ];

  let activeKey = $state("python");

  let activeProject = $derived(
    PROJECTS.find((p) => p.key === activeKey) ?? PROJECTS[0],
  );
</script>

<svelte:head><title>Firecracker Demos · private.jomcgi.dev</title></svelte:head>

<div class="demos">
  <header class="topbar">
    <span class="wordmark">Firecracker Demos</span>
    <nav class="topbar-nav" aria-label="Demo projects">
      {#each PROJECTS as project (project.key)}
        <button
          type="button"
          class="topbar-link"
          class:active={activeKey === project.key}
          onclick={() => (activeKey = project.key)}
        >
          {project.label}
        </button>
      {/each}
    </nav>
    <div class="topbar-spacer"></div>
  </header>

  <main class="demos-shell">
    <div class="demos-intro">
      <p class="demos-tagline">{activeProject.tagline}</p>
    </div>
    {#key activeProject.key}
      <RunPanel project={activeProject} />
    {/key}
  </main>
</div>

<style>
  .demos {
    background: var(--paper);
    color: var(--ink);
    min-height: 100vh;
    font-family: var(--font-sans, system-ui, sans-serif);
  }

  .topbar {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 0 28px;
    height: 58px;
    background: color-mix(in srgb, var(--paper) 88%, transparent);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--line);
  }

  .wordmark {
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    color: var(--ink);
    flex: none;
  }

  .topbar-nav {
    display: flex;
    gap: 4px;
    margin-left: 8px;
  }

  .topbar-link {
    display: inline-flex;
    align-items: center;
    min-height: 40px;
    padding: 6px 12px;
    margin-bottom: -1px;
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--text-faint);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
  }

  .topbar-link:hover {
    color: var(--text-dim);
  }

  .topbar-link.active {
    color: var(--ink);
    border-bottom-color: var(--accent);
  }

  .topbar-spacer {
    flex: 1;
  }

  .demos-shell {
    max-width: 60rem;
    margin: 0 auto;
    padding: 28px 28px 64px;
  }

  .demos-intro {
    margin-bottom: 20px;
  }

  .demos-tagline {
    font-size: 14px;
    line-height: 1.6;
    color: var(--text-dim);
    margin: 0;
  }

  @media (max-width: 640px) {
    .topbar {
      padding: 0 16px;
      gap: 12px;
    }
    .wordmark {
      font-size: 12px;
      letter-spacing: 0.2em;
    }
    .topbar-link {
      padding: 6px 8px;
      font-size: 10px;
    }
    .demos-shell {
      padding: 20px 16px 48px;
    }
  }
</style>
