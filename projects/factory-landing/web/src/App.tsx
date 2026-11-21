import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
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
      <VStack gap={6}>
        <header>
          <Text type="body">Homelab</Text>
          <Heading level={1}>Factory</Heading>
          <Text type="body">Your tasks and the factory's latest work.</Text>
        </header>
        {failed && (
          <p role="status">
            Factory status is unavailable. You can still open the tools below.
          </p>
        )}
        {!status && !failed && <p role="status">Loading factory status…</p>}
        <section aria-labelledby="needs-you">
          <Card>
            <VStack gap={3}>
              <Heading level={2} id="needs-you">
                Needs you
                {status?.needsYou ? ` (${status.needsYou.length})` : ""}
              </Heading>
              {status &&
                (status.needsYou === null ? (
                  <Text type="body">
                    Unavailable: no itsaplan member @joe in the factory's
                    project.
                  </Text>
                ) : status.needsYou.length ? (
                  <ul className="factory-list">
                    {status.needsYou.map((item) => (
                      <li key={item.key}>
                        <a href={item.url}>
                          {item.key}: {item.title}
                        </a>
                        <Text type="body">
                          {item.priority || "No priority"} · Created{" "}
                          <time dateTime={item.createdAt}>
                            {date(item.createdAt)}
                          </time>
                        </Text>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Text type="body">Nothing needs you right now.</Text>
                ))}
            </VStack>
          </Card>
        </section>
        <section aria-labelledby="in-flight">
          <Card>
            <VStack gap={3}>
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
                        <Text type="body">
                          {item.key}: {item.title}
                        </Text>
                        <Text type="body">
                          {label(item.state)} · Review rounds:{" "}
                          {item.reviewRounds}
                        </Text>
                        {item.pr ? (
                          <>
                            <a href={item.pr.url}>PR #{item.pr.number}</a>
                            <Text type="body">
                              Checks: {label(item.pr.ci)} · Review:{" "}
                              {label(item.pr.review)} · Queue:{" "}
                              {label(item.pr.queue)}
                            </Text>
                          </>
                        ) : (
                          <Text type="body">No pull request yet.</Text>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Text type="body">No work in flight.</Text>
                ))}
            </VStack>
          </Card>
        </section>
        <section aria-labelledby="shipped">
          <Card>
            <VStack gap={3}>
              <Heading level={2} id="shipped">
                Shipped recently
              </Heading>
              {status &&
                (status.shipped.length ? (
                  <ul className="factory-list">
                    {status.shipped.map((item) => (
                      <li key={item.key}>
                        <a href={item.pr.url}>
                          {item.key}: {item.title} (#{item.pr.number})
                        </a>
                        <Text type="body">
                          Merged{" "}
                          <time dateTime={item.mergedAt}>
                            {date(item.mergedAt)}
                          </time>
                        </Text>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Text type="body">No recent merges.</Text>
                ))}
            </VStack>
          </Card>
        </section>
        {status && (
          <Text type="body">
            Updated{" "}
            <time dateTime={status.generatedAt}>
              {new Date(status.generatedAt).toLocaleTimeString()}
            </time>
            . Refreshes every 30 seconds.
          </Text>
        )}
        <div className="factory-tools">
          {tools.map((tool) => (
            <Card key={tool.name}>
              <VStack gap={3}>
                <Heading level={2}>{tool.name}</Heading>
                <Text type="body">{tool.purpose}</Text>
                <HStack gap={2} wrap="wrap">
                  <Button label={`Open ${tool.name}`} href={tool.href} />
                  {tool.name === "GitHub" && (
                    <Button
                      label="Open factory PRs"
                      href={paths.pullRequests}
                    />
                  )}
                </HStack>
              </VStack>
            </Card>
          ))}
        </div>
      </VStack>
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
