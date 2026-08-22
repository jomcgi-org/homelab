// Single source of truth for public-tier SEO and machine-readable identity.
//
// `PUBLIC_BASE` is the canonical absolute origin for the public site. It is
// hardcoded (matching Nav.svelte's convention) rather than derived from the
// request so canonical/OG/sitemap URLs stay stable regardless of which host
// the gateway routes through. The public content lives on the apex; change
// this one constant if it ever moves again.
export const PUBLIC_BASE = "https://jomcgi.dev";

export const LOCATION = {
  region: "Scotland",
  country: "United Kingdom",
  countryCode: "GB",
  short: "Scotland",
};

// Schema.org Person: the highest-leverage structured data for a job search.
// It lets an LLM answer "who is Joe McGinley" from a typed entity (jobTitle,
// worksFor, sameAs) instead of parsing prose. Facts here are deliberately
// stable identity claims, kept independent of the CV page's presentation data.
export const person = {
  "@context": "https://schema.org",
  "@type": "Person",
  name: "Joe McGinley",
  jobTitle: "Senior Platform Engineer",
  worksFor: {
    "@type": "Organization",
    name: "Semgrep",
    url: "https://semgrep.dev",
  },
  url: PUBLIC_BASE,
  email: "mailto:joe@jomcgi.dev",
  address: {
    "@type": "PostalAddress",
    addressRegion: LOCATION.region,
    addressCountry: LOCATION.countryCode,
  },
  // sameAs links are how a model corroborates identity across the web; they
  // tie this page to the canonical LinkedIn/GitHub profiles.
  sameAs: ["https://www.linkedin.com/in/jomcgi/", "https://github.com/jomcgi"],
  knowsAbout: [
    "Kubernetes",
    "Platform Engineering",
    "Site Reliability Engineering",
    "eBPF",
    "Observability",
    "OpenTelemetry",
    "AWS",
    "Google Cloud Platform",
    "Go",
    "Python",
    "Distributed Systems",
  ],
  description:
    "Senior Platform Engineer running Kubernetes hands-on from ingress to eBPF: controllers, CRDs, observability, and per-customer cost attribution. AWS and GCP.",
};

// Pre-stringified <script> body for svelte:head. `<` is escaped to < so a
// stray "</script>" in any field can never break out of the script element.
export const personLdScript = `<script type="application/ld+json">${JSON.stringify(
  person,
).replace(/</g, "\\u003c")}</script>`;
