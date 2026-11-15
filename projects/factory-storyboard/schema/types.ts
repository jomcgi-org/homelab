export const states = [
  "Working",
  "In review",
  "In merge queue",
  "Shipped",
  "Stopped",
] as const;
export type State = (typeof states)[number];
export type ObjectPath = `objects/${string}.json`;
export interface Manifest {
  version: 1;
  generatedAt: string;
  story: ObjectPath;
  goals: Record<string, ObjectPath>;
  issues: Record<string, ObjectPath>;
  sessions: Record<string, ObjectPath>;
}
export interface Stat {
  label: string;
  value: number;
  unit: "issues" | "goals" | "hours" | "rounds" | "%";
}
export interface Point {
  label: string;
  value: number;
}
export interface GoalSummary {
  goalId: string;
  title: string;
  chapter: string[];
  state: State;
  shipped: number;
  total: number;
}
export interface IssueSummary {
  issueKey: string;
  goalId: string;
  title: string;
  state: State;
}
export interface Story {
  version: 1;
  kind: "story";
  headline: string;
  notice: string;
  narrative: string[];
  stats: Record<string, Stat>;
  goals: GoalSummary[];
  workInProgress: IssueSummary[];
  recentlyShipped: IssueSummary[];
  charts: {
    shippedPerWeek: Point[];
    issueToMergeHours: Point[];
    reviewRounds: Point[];
    firstPassReviewRate: Point[];
  };
}
export interface BurnPoint {
  date: string;
  shipped: number;
  total: number;
}
export interface Goal {
  version: 1;
  kind: "goal";
  id: string;
  title: string;
  state: State;
  chapter: string[];
  issueKeys: string[];
  burnUp: BurnPoint[];
}
type EventBase = { at: string; text: string };
export type TimelineEvent = EventBase &
  (
    | { kind: "opened" | "stopped" }
    | { kind: "session"; sessionId: string; turn: number }
    | { kind: "pullRequest" | "merged"; prNumber: number }
    | {
        kind: "review";
        prNumber: number;
        round: number;
        outcome: "Changes requested" | "Approved";
      }
    | { kind: "check"; name: string; result: "Passed" | "Failed" }
    | { kind: "mergeQueue"; state: "Entered" | "Left" }
  );
export interface Issue {
  version: 1;
  kind: "issue";
  key: string;
  goalId: string;
  title: string;
  state: State;
  description: string;
  timeline: TimelineEvent[];
}
export interface ToolCall {
  name: string;
  input: string;
  output: string;
}
export type Turn = {
  number: number;
  at: string;
  role: "user" | "assistant";
} & (
  | { withheld: true }
  | { withheld: false; text: string; toolCalls: ToolCall[]; diff?: string }
);
export interface Session {
  version: 1;
  kind: "session";
  id: string;
  issueKey: string;
  goalId: string;
  title: string;
  state: State;
  turns: Turn[];
}
export interface Documents {
  manifest: Manifest;
  story: Story;
  goal: Goal;
  issue: Issue;
  session: Session;
}
