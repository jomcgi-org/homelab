// CV content — transcribed verbatim from the canonical markdown CV
// (projects/websites/jomcgi.dev/src/assets/cv.md). Bullet strings keep the
// markdown inline syntax (**bold**, [text](url)); the page renders them via the
// tiny tokenizer in +page.svelte rather than pulling in a markdown dependency.

export const contact = {
  email: "joe@jomcgi.dev",
  linkedin: {
    label: "linkedin/jomcgi",
    href: "https://www.linkedin.com/in/jomcgi/",
  },
  github: { label: "github/jomcgi", href: "https://github.com/jomcgi" },
  location: "Vancouver",
};

export const name = "Joe McGinley";

export const summary =
  "As a Senior Platform Engineer, I build and operate reliable, cost-effective distributed systems, primarily using GCP and Kubernetes. I thrive on improving performance and stability, having drastically cut processing times (**weeks down to minutes**), reduced costs by up to **89%**, and eliminated recurring SLA violations. My approach relies on pragmatic automation, leveraging OpenTelemetry for deep system insights, and robust system design, especially for critical data platforms.";

export const jobs = [
  {
    company: "BenchSci",
    title: "Senior Software Engineer II (Promoted Oct 2023)",
    dates: "Oct 2022 – Present",
    bullets: [
      "Stabilized a critical, frequently failing product, reducing incident Time-to-Resolve (TTR) by **40%** and eliminating recurring SLA violations, by leading a cross-functional Root Cause Analysis (RCA) squad to produce an RCA playbook and a backlog of automated tests and reliability improvements for service owners.",
      "Reduced core document processing time from **weeks to minutes** by designing and building a scalable (25m+ docs) distributed event processing framework (Kubernetes/GKE), engineered for high resilience, scaling to cloud quota limits, and scaling to zero for cost efficiency enabling an **89%** reduction in processing costs.",
      "Optimized transactional write throughput for our primary **10TB** Postgres database utilizing PGvector (HNSW), enabling significantly faster ingestion of customer data while balancing high-performance reads under strict infrastructure scaling constraints.",
      "Managed and performance-tuned a large-scale Neo4j knowledge graph, optimizing complex query performance and data ingestion pipelines for essential company insights.",
      "Reduced incident Time-to-Identify (TTI) from **94 to 23 minutes** and cut false alerts by **15%** by driving company-wide OpenTelemetry adoption, creating a unified observability standard (logs, metrics, traces) and centralizing monitoring/alerting.",
      "Implemented an SLO framework translating business needs into measurable reliability targets, designed for low-friction developer adoption, guiding data-informed prioritization and clearly communicating essential non-functional requirements.",
      "Increased data release stability from bi-weekly failures to **>30 days** uptime using automated recovery and OpenTelemetry, allowing developers to ship features faster with reduced risk to users.",
      "Cut critical Postgres processing time by **55%** (to **9 hrs**) via tuning and scaling, enabling a faster release cadence and improving core software delivery performance metrics.",
      "Served as the go-to expert for data orchestration, resolving complex cross-team workflow challenges and ensuring successful platform adoption.",
    ],
  },
  {
    company: "Ensono",
    title: "Platform Engineering Consultant",
    dates: "May 2022 – Oct 2022",
    bullets: [
      "Architected and delivered a greenfield data platform on Google Cloud (GCP) for a major hotel chain, enabling self-service analytics for diverse stakeholders from HQ to individual hotel GMs.",
      "Engineered data ingestion and processing pipelines using Cloud Composer (Airflow), integrating robust data quality checks to ensure data integrity and reliability.",
      "Improved platform resilience and data availability through targeted architectural enhancements and operational best practices, supporting critical business decision-making.",
    ],
  },
  {
    company: "Hometree",
    title: "Senior Platform Engineer",
    dates: "Sep 2021 – May 2022",
    bullets: [
      "Optimized production database ER models to improve data access efficiency and simplify application development integration.",
      "Enhanced the resilience and operational reliability of a legacy data platform through targeted improvements and implementing engineering best practices.",
      "Modernized data modeling and transformation using DBT/BigQuery, increasing data consistency and visibility across the business.",
      "Provided data engineering guidance to Full Stack teams, enhancing data handling within core applications.",
    ],
  },
  {
    company: "AXA",
    title: "Senior Platform Engineer",
    dates: "Jan 2021 – Sep 2021",
    bullets: [
      "Designed robust batch and streaming ETL architectures enabling scalable data processing on Azure.",
      "Implemented automated infrastructure provisioning (Terraform) and deployment pipelines (CI/CD with Azure DevOps), improving platform stability and deployment velocity.",
      "Deployed key data services and infrastructure, including systems supporting ML applications that drove significant cost savings (e.g., **40%** CAC reduction).",
      "Consulted cross-functionally on data platform projects, influencing technical direction and implementation standards.",
    ],
  },
  {
    company: "Sky",
    title: "Platform Engineer",
    dates: "Feb 2020 – Jan 2021",
    bullets: [
      "Executed the migration of a core on-premise data platform to GCP, improving system scalability and unlocking new data exploration avenues.",
      "Designed and maintained robust, fault-tolerant ETL processes, increasing the resilience and reliability of critical data pipelines.",
      "Advanced team technical skills by mentoring engineers and analysts on development standards and new technologies.",
    ],
  },
];

export const projects = [
  "Actively contributing code and performing peer reviews for the OpenTelemetry project ([opentelemetry-python](https://github.com/open-telemetry/opentelemetry-python), [opentelemetry-python-contrib](https://github.com/open-telemetry/opentelemetry-python-contrib)).",
  "Designed and operate a bare-metal Kubernetes cluster (K3s) as a practical environment for reliability engineering experimentation.",
  "Centralized observability using the OpenTelemetry Collector to process diverse signals (traces, metrics, logs from cluster/apps, GitHub webhook -> Otel traces); data forwarded to Grafana Cloud and Honeycomb for analysis, alerting, and SLO tracking.",
  "Automated infrastructure and deployments using GitOps CI/CD principles, ensuring high availability awareness through layered monitoring: Uptime Kuma for on-site checks/alerts, backed by a GCP uptime check with SMS alerting monitoring the primary monitoring service itself.",
];

export const skills = [
  {
    label: "Cloud & Infrastructure",
    items: [
      "Google Cloud Platform (GCP)",
      "Azure",
      "Kubernetes (GKE)",
      "Terraform",
      "Infrastructure-as-Code (IaC)",
    ],
  },
  {
    label: "Reliability Engineering",
    items: [
      "SLO Definition & Implementation",
      "Incident Management & Post-mortems",
      "Monitoring & Alerting",
      "Chaos Engineering",
    ],
  },
  {
    label: "Observability",
    items: [
      "OpenTelemetry (OTel)",
      "Prometheus",
      "Grafana",
      "Distributed Tracing",
      "Structured Logging",
    ],
  },
  {
    label: "Development",
    items: [
      "Go",
      "Python",
      "Event-Driven Architecture",
      "Microservices",
      "API Design (REST)",
    ],
  },
  {
    label: "Databases & Data Systems",
    items: [
      "Postgres (incl. PGvector tuning)",
      "Neo4j",
      "BigQuery",
      "Data Modeling",
      "Database Optimization",
      "Cloud Pub/Sub",
      "ETL/Data Pipeline Design",
      "Data Orchestration (Airflow/Composer)",
    ],
  },
];
