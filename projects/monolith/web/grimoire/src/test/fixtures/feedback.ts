/** Invented feedback and the routes that serve it in tests and the preview. */
import type { FeedbackView } from "../../api/schema";
import type { Routes } from "../backend";
import { campaigns, ids } from "../fixtures";

export const feedbackIds = {
  dice: "f1111111-1111-4111-8111-111111111111",
  sheets: "f2222222-2222-4222-8222-222222222222",
};

export const feedbackItems: FeedbackView[] = [
  {
    id: feedbackIds.dice,
    created_at: "2026-11-11T19:42:00Z",
    author: "rowan@example.test",
    campaign_id: ids.joined,
    campaign_name: campaigns.joined.name,
    page: `/campaigns/${ids.joined}/session`,
    text: "Rolling with advantage shows both dice but not which one counted.\nCould the kept die be marked?",
    status: "new",
    issue_number: null,
  },
  {
    id: feedbackIds.sheets,
    created_at: "2026-11-10T08:05:00Z",
    author: "wren@example.test",
    campaign_id: null,
    campaign_name: null,
    page: "/sheets",
    text: "The spell list is hard to scan on a phone.",
    status: "new",
    issue_number: null,
  },
];

export const feedbackRoutes: Routes = {
  "GET /feedback": feedbackItems,
  "POST /feedback": { id: "f9999999-9999-4999-8999-999999999999" },
  "POST /feedback/{feedback_id}/file": {
    issue_number: 4321,
    issue_url: "https://github.example.test/issues/4321",
  },
  "POST /feedback/{feedback_id}/dismiss": { status: 204 },
};
