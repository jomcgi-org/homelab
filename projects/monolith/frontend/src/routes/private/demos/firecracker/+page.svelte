<script>
  // /demos/firecracker (private tier): a full-page, Grimoire-style tabbed
  // tool for the firecracker-backed projects (Python sandbox, Semgrep diff
  // scan, Goose agent, and the demo-postgres sleep/wake exhibit, which
  // renders its own PostgresPanel). Each other tab renders RunPanel full-page,
  // no modal: a modal close used to break mid-run because the dialog kept
  // its own escape/backdrop-click lifecycle independent of an in-flight
  // request, so we dropped the modal entirely in favor of a plain tabbed
  // page (mirrors the Grimoire app topbar in
  // src/routes/public/app/grimoire/+layout.svelte).
  import PostgresPanel from "$lib/private/components/demos/PostgresPanel.svelte";
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
from flask import Flask, request

app = Flask(__name__)


def read_target():
    # Taint SOURCE: an attacker-controlled query parameter.
    return request.args.get("host")


def ping(target):
    # Taint SINK: a shell command built from untrusted input.
    subprocess.run(f"ping -c1 {target}", shell=True)


@app.route("/ping")
def handle():
    # Source and sink live in different functions. Only Semgrep Pro's
    # interprocedural taint analysis connects them across this call; the
    # open-source engine sees each function alone and misses the flow.
    ping(read_target())
    return "ok"
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
      tagline:
        "Pro interprocedural taint scan: a source and sink in different functions, caught in the sandbox.",
      sample: { path: "app/ping.py", code: SEMGREP_SAMPLE },
    },
    {
      key: "goose",
      label: "Goose",
      tagline: "Kicks off an async agent task and polls it to completion.",
      sample: { task: GOOSE_TASK_SAMPLE },
    },
    {
      key: "postgres",
      label: "Postgres",
      tagline:
        "A scale-to-zero Postgres microVM: it falls asleep about a second after your last query and wakes on the next connection, data intact.",
      // No sample: this tab renders its own panel (status poll + timed
      // queries), not the shared RunPanel run/trace flow.
      sample: null,
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
      {#if activeProject.key === "postgres"}
        <PostgresPanel />
      {:else}
        <RunPanel project={activeProject} />
      {/if}
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
