import { METHOD_URL } from "./scotland-data.js";

// Privacy, terms and support for the Scotland connector, the pages the
// Claude and OpenAI directory listings link to (#7255). Each doc renders
// through PolicyPage.svelte. The facts behind the privacy page: the
// connector logs only internal errors (tool and error, no address); its
// meter counts calls by tool, surface and client class; the Envoy Gateway
// access log records each request's address, user agent and path with its
// query string, and no request bodies.

export const CONTACT_EMAIL = "joe@jomcgi.dev";
export const UPDATED = "2026-10-21";

export const policyLinks = [
  { href: "/scotland/privacy", label: "privacy" },
  { href: "/scotland/terms", label: "terms" },
  { href: "/scotland/support", label: "support" },
];

export const privacy = {
  slug: "privacy",
  title: "Privacy",
  lead: "What the Scotland connector sees when you use it, what it keeps and for how long.",
  description:
    "Privacy policy for the Scotland connector: no accounts, no cookies, no personal data asked for, and only short-lived request logs.",
  sections: [
    {
      id: "summary",
      label: "Summary",
      heading: "No accounts, nothing asked of you",
      paras: [
        "The connector is a free, read-only service run by Joe McGinley, an individual in Scotland. It has no accounts, sets no cookies, and never asks for your name, email or location history.",
        "A question reaches it as coordinates, dates, walk names and weather limits. It answers from forecast and climate data it has already fetched; your question is not passed on to MET Norway, Walkhighlands or anyone else.",
      ],
    },
    {
      id: "requests",
      label: "Requests",
      heading: "What a request carries",
      paras: [
        "Through a hosted assistant such as Claude or ChatGPT, the assistant's servers make the call, so the connector sees their network address and the tool arguments the assistant sends. Your conversation stays with the assistant.",
        "ChatGPT also attaches hints to each call: a coarse location (city, region, country and approximate coordinates), an anonymised user and conversation id, your locale and a user agent. The connector reads none of them, and since request bodies are never logged, none is kept.",
        "An assistant that runs on your own computer, such as Claude Code or Codex, calls from your machine, so your network address and its user agent reach the gateway. The same holds when you call the REST API directly, where the request itself is visible too: for plan, the point and dates in the URL.",
      ],
    },
    {
      id: "kept",
      label: "Kept",
      heading: "What is kept",
      paras: [
        "The gateway in front of the connector writes an access log line for each request: the time, the network address, the user agent, the path with its query string (so a plan's coordinates), the response status and size. Request bodies, which carry check and MCP arguments, are not logged. These logs are used only to keep the service running and to stop abuse, and are deleted on a rolling basis within 30 days.",
        "The connector itself counts calls by tool, by surface (MCP or REST) and by whether the caller is a known assistant, with no address or arguments attached, and logs internal errors with the tool name and the error. Rate limits count requests per network address in memory and keep nothing.",
      ],
    },
    {
      id: "others",
      label: "Others",
      heading: "Who else handles it",
      paras: [
        "Requests pass through Cloudflare, which carries the traffic and caches answers under its own privacy policy. The service and its logs run on Google Cloud. Neither gets anything from the connector beyond what carrying and hosting the traffic involves.",
        "Nothing is sold, shared for advertising, or used to train models.",
      ],
    },
    {
      id: "rights",
      label: "Your rights",
      heading: "Questions and requests",
      paras: [
        `To ask what is held about a network address you used, or to have it deleted, email ${CONTACT_EMAIL}. Since logs carry no account or name, include the address and roughly when you made the requests.`,
        "If this policy changes, the date at the top of the page changes with it.",
      ],
    },
  ],
};

export const terms = {
  slug: "terms",
  title: "Terms",
  lead: "The terms for using the Scotland connector's MCP server and REST API.",
  description:
    "Terms of use for the Scotland connector: free and read-only, provided as is, not a safety service, fair use and attribution.",
  sections: [
    {
      id: "service",
      label: "Service",
      heading: "A free service, as is",
      paras: [
        "The connector is free to use, with no sign-up or key. It is provided as is, without warranty, and can change, pause or stop at any time.",
        "Using it means accepting these terms. If you do not accept them, do not use it.",
      ],
    },
    {
      id: "outdoors",
      label: "Outdoors",
      heading: "It is not a safety service",
      paras: [
        "Answers come from forecasts, which can be wrong, and the weather is forecast at each walk's start; summits are usually colder and windier. A go is a suggestion, and whether a walk or a night is safe is yours to judge on the day.",
        "Decisions in the outdoors are yours. For hill days, check the Mountain Weather Information Service and conditions on the ground, and turn back when you need to.",
      ],
    },
    {
      id: "use",
      label: "Fair use",
      heading: "Use it for answers",
      paras: [
        "Ask it questions for yourself, your trips or the people your assistant helps. Do not use it to extract the underlying data in bulk, to rebuild or redistribute it, or to get round the rate limits.",
        "Requests over the limits are refused, and traffic that harms the service can be blocked.",
      ],
    },
    {
      id: "credit",
      label: "Credit",
      heading: "Credit the sources",
      paras: [
        "Answers draw on MET Norway, Copernicus CERRA, OpenStreetMap and Natural Earth under their own licences, and every response lists the sources to credit. Walk routes, maps and descriptions stay on Walkhighlands; each walk links to its page there.",
      ],
      rows: [{ k: "Method", v: "how verdicts are reached", href: METHOD_URL }],
    },
    {
      id: "liability",
      label: "Liability",
      heading: "Liability",
      paras: [
        "As far as the law allows, Joe McGinley is not liable for any loss arising from using the connector or relying on its answers.",
        "These terms are governed by the law of Scotland. If they change, the date at the top of the page changes with it.",
      ],
    },
  ],
};

export const support = {
  slug: "support",
  title: "Support",
  lead: "How to get help with the Scotland connector, or report something wrong.",
  description:
    "Support for the Scotland connector: how to report a problem or a wrong answer, and what to check first.",
  sections: [
    {
      id: "contact",
      label: "Contact",
      heading: "Get in touch",
      paras: [
        "Email is the way to reach a person. This is a one-person project, so replies are best effort, usually within a few days.",
      ],
      rows: [
        { k: "Email", v: CONTACT_EMAIL, href: `mailto:${CONTACT_EMAIL}` },
        { k: "Method", v: "how verdicts are reached", href: METHOD_URL },
      ],
    },
    {
      id: "report",
      label: "Report",
      heading: "Reporting a wrong answer",
      paras: [
        "Say what you asked, which assistant you used (or the REST URL), and roughly when. A wrong walk or site is easiest to trace with its name or id from the answer.",
      ],
    },
    {
      id: "first",
      label: "Check first",
      heading: "Before you write",
      paras: [
        "The hourly forecast reaches about two and a half days ahead; days past that come back as unknown, which is expected. Every answer says when its forecast was fetched.",
        "It covers Scotland only, and wants coordinates for the place you mean. An error that says the forecast data is loading clears within a minute or two.",
      ],
    },
  ],
};
