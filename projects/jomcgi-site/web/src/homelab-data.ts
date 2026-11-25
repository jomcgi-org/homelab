// Copy for /homelab, the living overview of the homelab repository.
//
// This page replaced the rendered Docs section. Per-project detail stays in
// the repository, where it is versioned with the code; this page says what
// runs today, how a threat is contained, and where to read more. Keep it at
// that altitude: no findings, hostnames or tracker ids. `public_content.py`
// guards the repository's public docs, and homelab.test.tsx applies the same
// markers to this copy.

export const REPO = "https://github.com/jomcgi-org/homelab";
export const repoFile = (path: string) => `${REPO}/blob/main/${path}`;
export const repoDir = (path: string, ref = "main") =>
  `${REPO}/tree/${ref}/${path}`;
/** The tag that keeps the code deleted with the cloud hub (HOME-262). */
export const HUB_FINAL = "hub-final";

/** The day someone last checked every statement on the page. Bump it only
 * after reading the page against the repository. */
export const REVIEWED = "2026-11-24";
/** A page older than this says so at the top. */
export const STALE_AFTER_DAYS = 90;

const DAY = 86_400_000;
/** Whole days from the review date to `now`, never negative. */
export function reviewAge(reviewed: string, now: number): number {
  return Math.max(0, Math.floor((now - Date.parse(reviewed)) / DAY));
}
export const isStale = (reviewed: string, now: number) =>
  reviewAge(reviewed, now) > STALE_AFTER_DAYS;

export const intro =
  "A small cluster at home runs a software factory: agents take work from a tracker, open pull requests, and ship what passes review. This page is the current overview. The detail lives in the repository, and the history lives in the blog.";

export interface Path {
  id: string;
  title: string;
  /** One line on what the path is for. */
  summary: string;
  steps: string[];
}

/** The three routes that matter for safety, drawn as steps in order. */
export const paths: Path[] = [
  {
    id: "visit",
    title: "A visitor reads a page",
    summary: "Static files on Cloudflare. No request reaches a cluster.",
    steps: ["Visitor", "Cloudflare", "Static site"],
  },
  {
    id: "change",
    title: "A change reaches the cluster",
    summary: "What merges is what runs, so the merge is the guarded step.",
    steps: [
      "Pull request",
      "Checks and independent review",
      "Merge queue",
      "main",
      "ArgoCD",
      "Cluster",
    ],
  },
  {
    id: "task",
    title: "An agent does a task",
    summary:
      "The agent holds placeholders. Real credentials are added outside the sandbox.",
    steps: [
      "Task from the tracker",
      "Sandboxed agent",
      "Egress gateway",
      "Scoped credential",
      "GitHub",
    ],
  },
];

export const layout = [
  {
    title: "Home cluster",
    body: "Three small servers run the factory and its tools. Everything on them is deployed from Git.",
  },
  {
    title: "Cloud hub",
    body: "The older cluster in the cloud. Its services are moving to the home cluster one at a time.",
  },
  {
    title: "This site",
    body: "Static files served by a Cloudflare Worker. No script runs per request, and no public request reaches a cluster.",
  },
];

export interface Principle {
  id: string;
  title: string;
  body: string;
}

export const containmentIntro =
  "The design assumes something will go wrong: an agent reads hostile text, a dependency is compromised, a token leaks, a bad change merges. The question for each is how far it can reach. Controls are layered so that one failure stays in one place.";

export const principles: Principle[] = [
  {
    id: "edge",
    title: "Nothing listens openly",
    body: "Services reach the internet only through an outbound Cloudflare tunnel. The servers drop inbound traffic by default. Sign-in is enforced at the gateway for every browser app, and the one public API is a recorded decision.",
  },
  {
    id: "agents",
    title: "Agents hold nothing worth stealing",
    body: "Each agent runs in a gVisor sandbox with no cluster identity and only placeholder credentials. Outbound calls pass an egress gateway that adds the real credential. A token names one repository and its permissions, so a hijacked agent can act on that repository and nothing else.",
  },
  {
    id: "merge",
    title: "One merge is not enough",
    body: "The main branch needs passing checks and an independent review on the exact commit, through a merge queue. The reviewer is read-only and takes its rules from the base commit, so a change cannot loosen the rules that judge it. Only CI can raise deployment pins.",
  },
  {
    id: "services",
    title: "Each service reaches only what it needs",
    body: "Network policy denies by default. Each service has its own database credentials. Storage is claimed by namespace, and admission policies decide which namespaces may attach routes or claim buckets.",
  },
  {
    id: "text",
    title: "Untrusted text is data",
    body: "Issue text, web pages and logs are treated as hostile input. An agent that reads them gets narrow tools. The reviewer's agent is read-only.",
  },
  {
    id: "output",
    title: "Public output goes through one door",
    body: "The site publishes an allowlist of objects, and a CI check rejects internal addresses and credentials in public docs. Agent memory and tracker text are private and stay out of the public repository and site.",
  },
  {
    id: "operators",
    title: "Operators get short, narrow credentials",
    body: "Operator sessions use scoped, short-lived tokens. The admin certificate is for break-glass use only. Nodes hold no registry credentials, and a host firewall limits who can reach them.",
  },
  {
    id: "recovery",
    title: "Recovery is part of the design",
    body: "The cluster tolerates the loss of one server. Databases replicate and archive continuously, and a failed change rolls back by reverting it.",
  },
];

export interface Position {
  who: string;
  reach: string;
}

/** The attacker positions from the threat model, with the reach each has by
 * design. These are design claims, not test results. */
export const positions: Position[] = [
  {
    who: "Anonymous internet",
    reach: "Sign-in pages and one public API. There are no open inbound ports.",
  },
  {
    who: "A prompt-injected agent",
    reach:
      "One sandbox, the egress gateway and DNS. It can open a pull request, which still needs review.",
  },
  {
    who: "A compromised dependency",
    reach:
      "It can only arrive in a pull request, so it meets the same checks and review.",
  },
  {
    who: "One bad merge",
    reach:
      "The cluster, because what merges is what runs. This is why the merge is the most guarded step. A revert rolls it back.",
  },
  {
    who: "A compromised pod",
    reach:
      "Its own network position, identity and data. Policy denies the rest by default.",
  },
  {
    who: "A device on the home network",
    reach:
      "The host firewall drops inbound traffic, and operator credentials are short-lived.",
  },
];

export const limits = [
  "The factory does not have its own STPA security analysis yet. It is planned.",
  "Findings from audits are tracked privately and fixed through the normal path. They are not listed here.",
];

export const method = {
  body: "Deep analysis uses STPA, which starts from the losses that matter and asks how each control could fail, including the ones that look sound. The monolith and the retired EmberVM sandbox each have one. The factory's is next.",
  links: [
    { label: "Threat model", href: repoFile("docs/THREAT-MODEL.md") },
    { label: "Security controls", href: repoFile("docs/security.md") },
  ],
};

export type Status = "live" | "moving" | "experiment" | "retired";
export const STATUS_LABEL: Record<Status, string> = {
  live: "Live",
  moving: "Moving",
  experiment: "Experiment",
  retired: "Retired",
};

export interface Project {
  name: string;
  what: string;
  status: Status;
  /** A directory in the repository. */
  path: string;
  /** The git ref that holds the directory, when it is not on main. */
  ref?: string;
}

export interface ProjectGroup {
  title: string;
  projects: Project[];
}

export const groups: ProjectGroup[] = [
  {
    title: "The factory",
    projects: [
      {
        name: "factory",
        what: "Takes work from the tracker, runs coding agents in sandboxes, and reviews and lands pull requests.",
        status: "live",
        path: "projects/factory",
      },
      {
        name: "factory-cluster",
        what: "The three-server home cluster: its edge, storage and recovery.",
        status: "live",
        path: "projects/factory-cluster",
      },
      {
        name: "platform-factory",
        what: "The platform apps that ArgoCD deploys to the home cluster.",
        status: "live",
        path: "projects/platform-factory",
      },
      {
        name: "factory-storyboard",
        what: "The snapshot contract and fixtures behind the public Factory page.",
        status: "live",
        path: "projects/factory-storyboard",
      },
      {
        name: "capacity-forecast",
        what: "Forecasts memory use for the reviewer pool.",
        status: "live",
        path: "projects/capacity-forecast",
      },
    ],
  },
  {
    title: "Public apps",
    projects: [
      {
        name: "jomcgi-site",
        what: "This site, built as static files and served by a Cloudflare Worker.",
        status: "live",
        path: "projects/jomcgi-site",
      },
      {
        name: "hikes",
        what: "Scottish walks with their weather windows.",
        status: "live",
        path: "projects/hikes",
      },
      {
        name: "stars-forecast",
        what: "Dark-sky sites and the nights worth going out.",
        status: "live",
        path: "projects/stars-forecast",
      },
      {
        name: "scotland-connector",
        what: "Walk and stargazing answers for AI assistants, over a public MCP endpoint.",
        status: "live",
        path: "projects/scotland-connector",
      },
      {
        name: "trips",
        what: "Trips day by day, with photos.",
        status: "moving",
        path: "projects/trips",
      },
      {
        name: "grimoire",
        what: "A reader and campaign manager for open tabletop rules.",
        status: "moving",
        path: "projects/grimoire",
      },
    ],
  },
  {
    title: "The cloud hub",
    projects: [
      {
        name: "monolith",
        what: "The personal knowledge graph, chat agent and tools behind most of the older services.",
        status: "moving",
        path: "projects/monolith",
      },
      {
        name: "mcp",
        what: "The gateway that lets AI clients reach the monolith's tools with one sign-in.",
        status: "retired",
        path: "projects/mcp",
        ref: HUB_FINAL,
      },
    ],
  },
  {
    title: "Tools and experiments",
    projects: [
      {
        name: "sextant",
        what: "Generates type-safe Go state machines for Kubernetes operators, so an invalid transition is a compile error.",
        status: "retired",
        path: "projects/sextant",
        ref: HUB_FINAL,
      },
      {
        name: "oom-inference",
        what: "Serves a 125B-parameter model from a 24 GB GPU. See the blog post.",
        status: "retired",
        path: "projects/oom-inference",
        ref: HUB_FINAL,
      },
      {
        name: "operators",
        what: "Keeps the OCI model cache operator, which mounts Hugging Face models from a registry. It has no deployment today.",
        status: "retired",
        path: "projects/operators",
        ref: HUB_FINAL,
      },
      {
        name: "model-bench",
        what: "Screened budget models on tasks from this repository. Production trials replace it.",
        status: "retired",
        path: "projects/model-bench",
        ref: HUB_FINAL,
      },
      {
        name: "embervm",
        what: "Firecracker orchestration, a private Lambda on owned metal. Retired in November 2026. The blog keeps its story.",
        status: "retired",
        path: "projects/embervm",
        ref: HUB_FINAL,
      },
    ],
  },
];

export const allProjects = groups.flatMap((group) => group.projects);
