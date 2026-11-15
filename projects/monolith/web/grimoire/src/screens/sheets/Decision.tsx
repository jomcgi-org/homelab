import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { TextArea } from "@astryxdesign/core/TextArea";
import { keys as lobbyKeys } from "../../api/lobby";
import { BOUNDS, decideSheet, type SheetVersion } from "../../api/sheets";
import { useAction } from "../../app/useAction";

interface DecisionProps {
  campaignId: string;
  characterId: string;
  version: SheetVersion;
}

/** The DM's ruling on a submitted version. Returning needs a reason. */
export function Decision({ campaignId, characterId, version }: DecisionProps) {
  const [comment, setComment] = useState("");
  const [missing, setMissing] = useState(false);
  const decide = useAction(decideSheet, {
    // Approval also updates the character's class and level.
    invalidate: [lobbyKeys.characters(campaignId)],
    success: (_result, input) =>
      input.decision === "approve"
        ? "Sheet approved."
        : "Sheet returned to the player.",
  });
  const run = (decision: "approve" | "return") => {
    const text = comment.trim().split(/\s+/).join(" ");
    if (decision === "return" && !text) {
      setMissing(true);
      return;
    }
    decide.run({
      campaignId,
      characterId,
      versionId: version.id,
      decision,
      comment: text || null,
    });
  };
  return (
    <VStack gap={3}>
      <TextArea
        label="Decision comment"
        description="Required when returning."
        value={comment}
        onChange={(value) => {
          setComment(value.slice(0, BOUNDS.comment));
          setMissing(false);
        }}
        maxLength={BOUNDS.comment}
        status={
          missing
            ? { type: "error", message: "Say what the player should change." }
            : undefined
        }
      />
      <HStack gap={2} wrap="wrap">
        <Button
          label="Approve version"
          variant="primary"
          isDisabled={decide.isPending}
          onClick={() => run("approve")}
        />
        <Button
          label="Return to player"
          isDisabled={decide.isPending}
          onClick={() => run("return")}
        />
      </HStack>
    </VStack>
  );
}
