/** Sending feedback, and the operator's private triage inbox. */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export const keys = {
  inbox: ["feedback", "new"] as const,
};

/** Operators only: anyone else is refused with a 403. */
export const useFeedbackInbox = () =>
  useQuery({
    queryKey: keys.inbox,
    queryFn: () => api("GET /feedback", { query: { status: "new" } }),
  });

export const sendFeedback = (input: {
  text: string;
  /** The in-app path the feedback was written on. */
  page: string;
  campaignId?: string;
}) =>
  api("POST /feedback", {
    body: {
      text: input.text,
      page: input.page,
      ...(input.campaignId ? { campaign_id: input.campaignId } : {}),
    },
  });

/** Publishes the given title and body, and nothing else, as a GitHub issue. */
export const fileFeedback = (input: {
  id: string;
  title: string;
  body: string;
}) =>
  api("POST /feedback/{feedback_id}/file", {
    params: { feedback_id: input.id },
    body: { title: input.title, body: input.body },
  });

export const dismissFeedback = (id: string) =>
  api("POST /feedback/{feedback_id}/dismiss", { params: { feedback_id: id } });
