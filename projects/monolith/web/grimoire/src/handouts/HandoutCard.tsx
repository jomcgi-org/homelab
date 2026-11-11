import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { handoutBody, handoutImageUrl } from "../api/handouts";
import type { SessionEvent } from "../api/session";
import { HandoutText } from "./HandoutText";
import "./handouts.css";

export interface HandoutCardProps {
  campaignId: string;
  sessionId: string;
  handout: SessionEvent;
  dm: boolean;
  /** Pin this handout to the viewer's notes. */
  onPin: (event: SessionEvent) => void;
  onOpenKnowledge: (entityId: string) => void;
  /** The title's heading level; headings in the text sit below it. */
  headingLevel?: 3 | 4;
}

/** A handout in the feed: title, text, image and linked knowledge. */
export function HandoutCard({
  campaignId,
  sessionId,
  handout,
  dm,
  onPin,
  onOpenKnowledge,
  headingLevel = 3,
}: HandoutCardProps) {
  const body = handoutBody(handout);
  if (handout.retracted_at || !body || body.retracted) {
    return (
      <Text type="body" color="secondary">
        This handout was retracted.
      </Text>
    );
  }
  const title = body.title || "Handout";
  const entityId = body.entity_id;
  return (
    <VStack gap={2}>
      <Heading level={headingLevel}>{title}</Heading>
      {body.image && (
        <img
          className="handout-image"
          src={handoutImageUrl(campaignId, sessionId, handout.id)}
          alt={`Handout image: ${title}`}
          loading="lazy"
        />
      )}
      <HandoutText
        markdown={body.markdown}
        headingLevel={(headingLevel + 1) as 4 | 5}
      />
      {(entityId || !dm) && (
        <HStack gap={2} wrap="wrap">
          {entityId && (
            <Button
              label={`Explore knowledge linked to ${title}`}
              size="sm"
              onClick={() => onOpenKnowledge(entityId)}
            >
              Explore linked knowledge
            </Button>
          )}
          {!dm && (
            <Button
              label={`Pin ${title} to my notes`}
              size="sm"
              variant="ghost"
              onClick={() => onPin(handout)}
            >
              Pin to my notes
            </Button>
          )}
        </HStack>
      )}
    </VStack>
  );
}
