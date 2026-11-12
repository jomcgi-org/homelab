import { Fragment, useState, type ReactNode } from "react";
import { HStack, StackItem, VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Text } from "@astryxdesign/core/Text";
import { ToggleButton } from "@astryxdesign/core/ToggleButton";
import { keys, setVote, useRoadmap } from "../../api/feedback";
import type { RoadmapItem } from "../../api/schema";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { useAction } from "../../app/useAction";
import { NoteText } from "../../notes/NoteText";

type State = RoadmapItem["state"];

const tabs: { state: State; label: string }[] = [
  { state: "new", label: "New" },
  { state: "accepted", label: "Accepted" },
  { state: "completed", label: "Completed" },
  { state: "rejected", label: "Rejected" },
];
const PANEL = "roadmap-panel";
const open = (state: State) => state === "new" || state === "accepted";

/**
 * What everyone has suggested and what happened to it. Friends check here
 * before sending a note, and add "Me too" to one that is already listed.
 */
export function Roadmap() {
  const roadmap = useRoadmap();
  return (
    <Page
      title="Roadmap"
      back={{ href: paths.lobby, label: "Back to Grimoire" }}
    >
      <Loaded query={roadmap} what="the roadmap">
        {(items) => <Items items={items} />}
      </Loaded>
    </Page>
  );
}

function Items({ items }: { items: RoadmapItem[] }) {
  const [state, setState] = useState<State>("new");
  const shown = items.filter((item) => item.state === state);
  const current = tabs.find((tab) => tab.state === state)!;
  return (
    <VStack gap={5}>
      <TabList
        role="tablist"
        aria-label="Roadmap"
        value={state}
        onChange={(value) => setState(value as State)}
      >
        {tabs.map((tab) => (
          <Tab
            key={tab.state}
            value={tab.state}
            panelId={PANEL}
            label={`${tab.label} (${items.filter((item) => item.state === tab.state).length})`}
          />
        ))}
      </TabList>
      <div id={PANEL} role="tabpanel" aria-label={current.label}>
        {shown.length === 0 ? (
          <Text type="body" color="secondary">
            Nothing here yet.
          </Text>
        ) : (
          <VStack gap={4} as="ul" className="note-list">
            {shown.map((item) => (
              <li key={item.id}>
                <Row item={item} />
              </li>
            ))}
          </VStack>
        )}
      </div>
    </VStack>
  );
}

const count = (votes: number) =>
  votes === 1 ? "1 person agrees" : `${votes} people agree`;

function Row({ item }: { item: RoadmapItem }) {
  const vote = useAction(setVote, { invalidate: [keys.roadmap] });
  // An author cannot vote for their own item: the count is other people.
  const votable = open(item.state) && !item.mine;
  const details: ReactNode[] = [];
  if (item.mine) details.push("Yours");
  if (!votable && item.votes > 0) details.push(count(item.votes));
  if (item.issue_url)
    details.push(
      <Link href={item.issue_url} isExternalLink size="sm">
        Issue on GitHub
      </Link>,
    );
  return (
    <HStack gap={4} vAlign="start">
      <StackItem size="fill">
        <VStack gap={1}>
          <NoteText text={item.text} />
          {item.reason && (
            <Text type="body" color="secondary">
              {item.reason}
            </Text>
          )}
          {details.length > 0 && (
            <Text type="supporting">
              {details.map((detail, index) => (
                <Fragment key={index}>
                  {index > 0 && " · "}
                  {detail}
                </Fragment>
              ))}
            </Text>
          )}
        </VStack>
      </StackItem>
      {votable && (
        <StackItem size="static">
          <ToggleButton
            label={`Me too (${item.votes})`}
            size="sm"
            isPressed={item.voted}
            isDisabled={vote.isPending}
            onPressedChange={(voted) => vote.run({ id: item.id, voted })}
          />
        </StackItem>
      )}
    </HStack>
  );
}
