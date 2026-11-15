import type { SheetVersion } from "../../api/sheets";

type Status = SheetVersion["status"] | undefined;

const labels: Record<SheetVersion["status"], string> = {
  draft: "Draft",
  submitted: "Submitted",
  approved: "Approved",
  returned: "Returned",
};

export const statusLabel = (status: Status) =>
  status ? labels[status] : "No sheet";

export const statusVariant = (status: Status) =>
  status === "approved"
    ? "success"
    : status === "submitted"
      ? "info"
      : status === "returned"
        ? "warning"
        : "neutral";
