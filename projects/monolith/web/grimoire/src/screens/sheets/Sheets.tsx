import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { LobbyCampaignView } from "../../api/schema";
import { useCharacters, useLobby } from "../../api/lobby";
import { Loaded } from "../../app/Loaded";
import { Page } from "../../app/Page";
import { paths } from "../../app/paths";
import { CharacterSheet } from "./CharacterSheet";

/** Character sheets: drafts, submission and the DM's approval. */
export function Sheets() {
  const lobby = useLobby();
  return (
    <Page
      title="Character sheets"
      back={{ href: paths.lobby, label: "Your campaigns" }}
    >
      <Loaded query={lobby} what="character sheets">
        {(data) =>
          data.campaigns.length === 0 ? (
            <EmptyState
              isCompact
              headingLevel={2}
              title="No campaigns yet"
              description="A campaign DM must add your verified account before sheets appear here."
            />
          ) : (
            <>
              <Text type="body" color="secondary">
                You choose your character's ancestry, class, level and ability
                scores, Grimoire works out the numbers, and your DM approves the
                sheet.
              </Text>
              {data.campaigns.map((campaign) => (
                <CampaignSheets key={campaign.id} campaign={campaign} />
              ))}
            </>
          )
        }
      </Loaded>
    </Page>
  );
}

/** One campaign's characters: all of them for the DM, their own for a player. */
function CampaignSheets({ campaign }: { campaign: LobbyCampaignView }) {
  const characters = useCharacters(campaign.id);
  const headingId = `campaign-${campaign.id}`;
  return (
    <VStack gap={3} as="section" aria-labelledby={headingId}>
      <VStack gap={1}>
        <Text type="label" color="secondary">
          {campaign.role === "dm" ? "DM" : "Player"}
        </Text>
        <Heading level={2} id={headingId}>
          {campaign.name}
        </Heading>
      </VStack>
      <Loaded query={characters} what="this campaign's characters">
        {(rows) =>
          rows.length === 0 ? (
            <Text type="body" color="secondary">
              No assigned characters.
            </Text>
          ) : (
            rows.map((character) => (
              <CharacterSheet key={character.id} character={character} />
            ))
          )
        }
      </Loaded>
    </VStack>
  );
}
