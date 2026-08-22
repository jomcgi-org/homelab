export const PHASES = {
  heroOut: [0, 0.055],
  wake: [0.07, 0.25],
  hydrate: [0.28, 0.44],
  creds: [0.47, 0.63],
  park: [0.66, 0.79],
  resume: [0.82, 0.96],
  out: [0.96, 1],
};

export const CHAT_REVEAL = 0.022;
export const CHAT_SCHEDULE = [
  [0.08, "wake"],
  [0.14, "wake"],
  [0.3, "hydrate"],
  [0.37, "hydrate"],
  [0.5, "creds"],
  [0.57, "creds"],
  [0.71, "park"],
  [0.84, "resume"],
  [0.87, "resume"],
  [0.905, "resume"],
  [0.935, "resume"],
];
export const CHAT_AT = CHAT_SCHEDULE.map(([at]) => at);

export const CALLS = {
  wake: [
    {
      a: 0.02,
      b: 0.22,
      cls: "w-ember",
      text: "POST /v1/workloads/claude-runtime/sessions",
    },
    {
      a: 0.2,
      b: 0.45,
      cls: "w-ember",
      text: "PUT /snapshot/load  (base memfile)",
    },
    {
      a: 0.45,
      b: 0.58,
      cls: "w-amber",
      text: "PATCH /drives/volume  (workspace.img)",
    },
    { a: 0.58, b: 0.7, cls: "w-ember", text: "PATCH /vm  {state: Resumed}" },
  ],
  hydrate: [
    { a: 0.05, b: 0.25, cls: "w-good", text: "CONNECT vsock :1025" },
    {
      a: 0.3,
      b: 0.6,
      cls: "w-good",
      text: "git clone --filter=blob:none github.com/jomcgi/homelab",
    },
  ],
  creds: [
    { a: 0.15, b: 0.45, cls: "w-good", text: "CONNECT api.anthropic.com:443" },
    {
      a: 0.55,
      b: 0.85,
      cls: "w-good",
      text: "Authorization: Bearer ●●●  (swapped by the sidecar)",
    },
  ],
  park: [
    { a: 0.1, b: 0.42, cls: "w-frost", text: "idle 20 s → park" },
    {
      a: 0.45,
      b: 0.75,
      cls: "w-ember",
      text: "destroy vm  (no snapshot taken)",
    },
  ],
  resume: [
    {
      a: 0.04,
      b: 0.32,
      cls: "w-frost",
      text: "PUT workspace snapshot to object store",
    },
    {
      a: 0.42,
      b: 0.66,
      cls: "w-frost",
      text: "GET workspace snapshot → new node",
    },
    { a: 0.68, b: 0.82, cls: "w-ember", text: "PUT /snapshot/load (new node)" },
    {
      a: 0.84,
      b: 0.96,
      cls: "",
      text: "claude --resume  (conversation intact)",
    },
  ],
};

export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const sub = (t, a, b) => clamp((t - a) / (b - a), 0, 1);
export const easeInOut = (p) =>
  p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2;
