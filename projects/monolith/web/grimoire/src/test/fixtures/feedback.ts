/** Invented feedback and the routes that serve it in tests and the preview. */
import type { FeedbackView, RoadmapItem } from "../../api/schema";
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
    votes: 2,
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
    votes: 0,
  },
];

export const roadmapIds = {
  dice: feedbackIds.dice,
  sheets: feedbackIds.sheets,
  search: "f3333333-3333-4333-8333-333333333333",
  initiative: "f4444444-4444-4444-8444-444444444444",
  handouts: "f5555555-5555-4555-8555-555555555555",
  sounds: "f6666666-6666-4666-8666-666666666666",
};

const issue = (number: number) =>
  `https://github.example.test/issues/${number}`;

/** One item in every state. The caller wrote `sheets`. */
export const roadmapItems: RoadmapItem[] = [
  {
    id: roadmapIds.dice,
    state: "new",
    text: feedbackItems[0]!.text,
    votes: 2,
    voted: false,
    mine: false,
    created_at: "2026-11-11T19:42:00Z",
    issue_url: null,
    reason: null,
  },
  {
    id: roadmapIds.sheets,
    state: "new",
    text: feedbackItems[1]!.text,
    votes: 1,
    voted: false,
    mine: true,
    created_at: "2026-11-10T08:05:00Z",
    issue_url: null,
    reason: null,
  },
  {
    id: roadmapIds.search,
    state: "accepted",
    text: "Search across campaign notes",
    votes: 3,
    voted: true,
    mine: false,
    created_at: "2026-11-08T17:20:00Z",
    issue_url: issue(4310),
    reason: null,
  },
  {
    id: roadmapIds.initiative,
    state: "completed",
    text: "Show whose turn is next in the initiative order",
    votes: 4,
    voted: false,
    mine: false,
    created_at: "2026-11-02T12:00:00Z",
    issue_url: issue(4275),
    reason: null,
  },
  {
    id: roadmapIds.handouts,
    state: "rejected",
    text: "Let players edit handouts.",
    votes: 0,
    voted: false,
    mine: false,
    created_at: "2026-10-30T09:15:00Z",
    issue_url: null,
    reason: "Handouts stay with the DM. Notes are the place for player text.",
  },
  {
    id: roadmapIds.sounds,
    state: "rejected",
    text: "Ambient sound for each scene",
    votes: 1,
    voted: false,
    mine: false,
    created_at: "2026-10-28T21:40:00Z",
    issue_url: issue(4250),
    reason: null,
  },
];

export const feedbackRoutes: Routes = {
  "GET /feedback": feedbackItems,
  "GET /feedback/roadmap": roadmapItems,
  "PUT /feedback/{feedback_id}/vote": { votes: 3, voted: true },
  "DELETE /feedback/{feedback_id}/vote": { votes: 2, voted: false },
  "POST /feedback": { id: "f9999999-9999-4999-8999-999999999999" },
  "POST /feedback/{feedback_id}/file": {
    issue_number: 4321,
    issue_url: "https://github.example.test/issues/4321",
  },
  "POST /feedback/{feedback_id}/dismiss": { status: 204 },
};
