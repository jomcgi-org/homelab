import { useState } from "react";
import { TextInput } from "@astryxdesign/core/TextInput";

const MAX_REASON = 500;

/** The reason every account change records in the audit trail. */
export function useReason() {
  const [value, setValue] = useState("");
  const [isMissing, setMissing] = useState(false);
  return {
    value,
    isMissing,
    set: (next: string) => {
      setValue(next.slice(0, MAX_REASON));
      setMissing(false);
    },
    /** The trimmed reason, or null after marking the empty field. */
    take: (): string | null => {
      const reason = value.trim();
      setMissing(!reason);
      return reason || null;
    },
  };
}

export function ReasonField({
  reason,
}: {
  reason: ReturnType<typeof useReason>;
}) {
  return (
    <TextInput
      label="Reason"
      description="Recorded with the change."
      isRequired
      value={reason.value}
      onChange={reason.set}
      status={
        reason.isMissing
          ? { type: "error", message: "Give a reason first." }
          : undefined
      }
      width="100%"
    />
  );
}
