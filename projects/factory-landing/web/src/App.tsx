import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { paths } from "./paths";

const tools = [
  { name: "itsaplan", purpose: "Work and the run queue.", href: paths.plan },
  {
    name: "kagent",
    purpose: "Agent sessions.",
    href: paths.agents,
    comingSoon: true,
  },
  { name: "Grafana", purpose: "Metrics.", href: paths.grafana },
  { name: "ArgoCD", purpose: "Deploy state.", href: paths.argocd },
  { name: "GitHub", purpose: "Source and pull requests.", href: paths.github },
];

export function App() {
  return (
    <main className="factory-page">
      <VStack gap={6}>
        <header>
          <Text type="body">Homelab / operator tools</Text>
          <Heading level={1}>Factory</Heading>
          <Text type="body">Choose a tool to open its workspace.</Text>
        </header>
        <div className="factory-tools">
          {tools.map((tool) => (
            <Card key={tool.name}>
              <VStack gap={3}>
                <Heading level={2}>{tool.name}</Heading>
                <Text type="body">{tool.purpose}</Text>
                {tool.comingSoon ? (
                  <>
                    <Text type="body">Coming soon</Text>
                    <Text type="body">{tool.href}</Text>
                  </>
                ) : (
                  <HStack gap={2} wrap="wrap">
                    <Button label={`Open ${tool.name}`} href={tool.href} />
                    {tool.name === "GitHub" && (
                      <Button
                        label="Open factory PRs"
                        href={paths.pullRequests}
                      />
                    )}
                  </HStack>
                )}
              </VStack>
            </Card>
          ))}
        </div>
      </VStack>
    </main>
  );
}
