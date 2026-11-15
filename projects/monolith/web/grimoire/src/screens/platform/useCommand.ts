import { useRef } from "react";
import {
  platformKeys,
  sendCommand,
  type Command,
  type CommandResult,
} from "../../api/platform";
import { useAction } from "../../app/useAction";

const SAVED: Partial<Record<Command["action"], string>> = {
  bootstrap: "Operator identity imported.",
  issue: "Invitation prepared. Manage it to show the signup link.",
  revoke_invitation: "Invitation revoked.",
  grant: "Permission granted.",
  revoke_grant: "Permission revoked.",
};

/**
 * Sends account changes. Each distinct change gets its own request id; trying
 * the same change again after a failure reuses it, so the backend applies a
 * change whose first answer was lost only once.
 */
export function useCommand(
  onSuccess?: (result: CommandResult, command: Command) => void,
) {
  const unsettled = useRef<{ change: string; id: string } | null>(null);
  return useAction(
    async (command: Command) => {
      const change = JSON.stringify(command);
      if (unsettled.current?.change !== change)
        unsettled.current = { change, id: crypto.randomUUID() };
      const result = await sendCommand({
        ...command,
        request_id: unsettled.current.id,
      });
      unsettled.current = null;
      return result;
    },
    {
      invalidate: [platformKeys.all],
      success: (_result, command) =>
        command.action === "set_active"
          ? command.active
            ? "Platform account enabled."
            : "Platform access disabled."
          : (SAVED[command.action] ?? ""),
      onSuccess,
    },
  );
}
