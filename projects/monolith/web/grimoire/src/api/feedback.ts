/** Sending feedback, the shared roadmap, and the operator's triage inbox. */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, UnexpectedResponseError } from "./client";

export const keys = {
  inbox: ["feedback", "new"] as const,
  roadmap: ["feedback", "roadmap"] as const,
};

/** Every signed-in user: what was suggested and what happened to it. */
export const useRoadmap = () =>
  useQuery({
    queryKey: keys.roadmap,
    queryFn: () => api("GET /feedback/roadmap"),
  });

/** Adds or withdraws the caller's "Me too" on someone else's item. */
export const setVote = (input: { id: string; voted: boolean }) =>
  api(
    input.voted
      ? "PUT /feedback/{feedback_id}/vote"
      : "DELETE /feedback/{feedback_id}/vote",
    { params: { feedback_id: input.id } },
  );

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
export const fileFeedback = async (input: {
  id: string;
  title: string;
  body: string;
}) => {
  try {
    return await api("POST /feedback/{feedback_id}/file", {
      params: { feedback_id: input.id },
      body: { title: input.title, body: input.body },
    });
  } catch (error) {
    // Keep delivered API refusals (including sign-in and conflict handling).
    // Transport, timeout and unreadable responses leave publication unknown.
    if (
      error instanceof ApiError &&
      !(error instanceof UnexpectedResponseError)
    )
      throw error;
    throw new ApiError(
      502,
      "GitHub did not confirm the issue. Check the repository before filing again.",
    );
  }
};

/** The reason, when given, is shown on the roadmap. */
export const dismissFeedback = (input: { id: string; reason?: string }) =>
  api("POST /feedback/{feedback_id}/dismiss", {
    params: { feedback_id: input.id },
    ...(input.reason ? { body: { reason: input.reason } } : {}),
  });
