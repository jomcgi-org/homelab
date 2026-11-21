import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { useEffect, useState } from "react";
import { paths } from "./paths";
import { parseStatus, type Status } from "./status";

const tools = [
  { name: "itsaplan", purpose: "Work and the run queue.", href: paths.plan },
  {
    name: "kagent",
    purpose: "Agent sessions.",
    href: paths.agents,
  },
  { name: "Grafana", purpose: "Metrics.", href: paths.grafana },
  { name: "ArgoCD", purpose: "Deploy state.", href: paths.argocd },
  { name: "GitHub", purpose: "Source and pull requests.", href: paths.github },
];

export function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let controller: AbortController | undefined;
    const refresh = async () => {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      try {
        const response = await fetch("/api/status", {
          signal: request.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Status unavailable");
        const next = parseStatus(await response.json());
        if (active && !request.signal.aborted) {
          setStatus(next);
          setFailed(false);
        }
      } catch {
        if (active && !request.signal.aborted) {
          setStatus(null);
          setFailed(true);
        }
      }
    };
    void refresh();
    const interval = setInterval(() => void refresh(), 30_000);
    return () => {
      active = false;
      controller?.abort();
      clearInterval(interval);
    };
  }, []);

  return (
    <main className="factory-page">
      <header className="factory-head">
        <div>
          <p className="eyebrow">Homelab</p>
          <Heading level={1}>Factory</Heading>
          <p className="meta">Your tasks and the factory's latest work.</p>
        </div>
        {status && (
          <p className="meta">
            Updated{" "}
            <time dateTime={status.generatedAt}>
              {new Date(status.generatedAt).toLocaleTimeString()}
            </time>
            . Refreshes every 30 seconds.
          </p>
        )}
      </header>
      {failed && (
        <p role="status" className="factory-notice">
          Factory status is unavailable. You can still open the tools below.
        </p>
      )}
      {!status && !failed && (
        <p role="status" className="factory-notice">
          Loading factory status…
        </p>
      )}
      <section aria-labelledby="needs-you" className="needs-you">
        <Heading level={2} id="needs-you">
          Needs you
          {status?.needsYou ? ` (${status.needsYou.length})` : ""}
        </Heading>
        {status &&
          (status.needsYou === null ? (
            <p className="meta">
              Unavailable: no itsaplan member @joe in the factory's project.
            </p>
          ) : status.needsYou.length ? (
            <ul className="factory-list">
              {status.needsYou.map((item) => (
                <li key={item.key}>
                  <a className="record" href={item.url}>
                    <span className="key">{item.key}:</span> {item.title}
                  </a>
                  <p className="meta">
                    {item.priority || "No priority"} · Created{" "}
                    <time dateTime={item.createdAt}>
                      {date(item.createdAt)}
                    </time>
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="meta">Nothing needs you right now.</p>
          ))}
      </section>
      <section aria-labelledby="in-flight">
        <Heading level={2} id="in-flight">
          In flight
          {status &&
            ` (${status.inFlight.length}${status.inFlightTotal ? ` of ${status.inFlightTotal}` : ""})`}
        </Heading>
        {status &&
          (status.inFlight.length ? (
            <ul className="factory-list">
              {status.inFlight.map((item) => (
                <li key={item.key}>
                  <p className="record">
                    <span className="key">{item.key}:</span> {item.title}
                  </p>
                  <div className="detail">
                    <p className="meta">
                      {label(item.state)} · Review rounds: {item.reviewRounds}
                    </p>
                    {item.pr ? (
                      <>
                        <a href={item.pr.url}>PR #{item.pr.number}</a>
                        <p className="meta">
                          Checks: {label(item.pr.ci)} · Review:{" "}
                          {label(item.pr.review)} · Queue:{" "}
                          {label(item.pr.queue)}
                        </p>
                      </>
                    ) : (
                      <p className="meta">No pull request yet.</p>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="meta">No work in flight.</p>
          ))}
      </section>
      <section aria-labelledby="shipped">
        <Heading level={2} id="shipped">
          Shipped recently
        </Heading>
        {status &&
          (status.shipped.length ? (
            <ul className="factory-list">
              {status.shipped.map((item) => (
                <li key={item.key}>
                  <a className="record" href={item.pr.url}>
                    <span className="key">{item.key}:</span> {item.title} (#
                    {item.pr.number})
                  </a>
                  <p className="meta">
                    Merged{" "}
                    <time dateTime={item.mergedAt}>{date(item.mergedAt)}</time>
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="meta">No recent merges.</p>
          ))}
      </section>
      <div className="factory-tools">
        {tools.map((tool) => (
          <section key={tool.name}>
            <Heading level={2}>{tool.name}</Heading>
            <p className="meta">{tool.purpose}</p>
            <div className="actions">
              <Button
                variant="ghost"
                size="sm"
                label={`Open ${tool.name}`}
                href={tool.href}
              />
              {tool.name === "GitHub" && (
                <Button
                  variant="ghost"
                  size="sm"
                  label="Open factory PRs"
                  href={paths.pullRequests}
                />
              )}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}

function label(value: string) {
  return value.replaceAll("_", " ");
}

function date(value: string) {
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
