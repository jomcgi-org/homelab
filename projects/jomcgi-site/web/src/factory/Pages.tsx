import { Heading } from "@astryxdesign/core/Heading";
import type {
  Goal,
  Issue,
  IssueSummary,
  Session,
  Stat,
  State,
  Story,
  TimelineEvent,
  Turn,
} from "../../../../factory-storyboard/schema/types";
import { states } from "../../../../factory-storyboard/schema/types";
import type { Snapshot } from "./data";
import {
  axisLabel,
  fillStats,
  formatInstant,
  formatShort,
  firstPassSentence,
  mergeSentence,
  roundsSentence,
  shippedSentence,
  weekSentence,
  paths,
  sessionLabel,
  tallyCounts,
  tallyHours,
} from "./data";
import { DashboardChart } from "../DashboardChart";
import { BurnUp } from "./Charts";
import { Text } from "./Text";

/** A long list shows this many rows; the rest open on request. */
const listLimit = 12;
/** A weekly chart shows at least this many weeks, so a new series starts
 * at the left and grows to the right. */
const weekSlots = 8;

export const withheldMessage =
  "This turn was withheld because it matched the secret scan.";
export function NotAvailable({ label }: { label: string }) {
  return (
    <p className="unavailable" role="status">
      {label} not available.
    </p>
  );
}
const lookup = <T,>(records: Record<string, T>, id: string): T | undefined =>
  Object.hasOwn(records, id) ? records[id] : undefined;
function Status({ state }: { state: State }) {
  return (
    <span className="state" data-state={state}>
      {state}
    </span>
  );
}
function Narrative({
  paragraphs,
  stats,
}: {
  paragraphs: string[];
  stats: Record<string, Stat>;
}) {
  return (
    <div className="narrative">
      {paragraphs.map((text, index) => (
        <p key={index}>{fillStats(text, stats)}</p>
      ))}
    </div>
  );
}
function Trail({ goalId, issueKey }: { goalId?: string; issueKey?: string }) {
  return (
    <nav className="trail" aria-label="Breadcrumb">
      <a href="/factory">Story</a>
      {goalId && <a href={paths.goal(goalId)}>Goal: {goalId}</a>}
      {issueKey && <a href={paths.issue(issueKey)}>{issueKey}</a>}
    </nav>
  );
}
function SectionHead({
  id,
  title,
  count,
}: {
  id: string;
  title: string;
  count?: number;
}) {
  return (
    <div className="section-head">
      <Heading level={2} id={id}>
        {title}
      </Heading>
      {count !== undefined && <p className="count">{count}</p>}
    </div>
  );
}
function IssueRows({
  issues,
  snapshot,
  showGoal,
}: {
  issues: IssueSummary[];
  snapshot?: Snapshot;
  showGoal: boolean;
}) {
  return (
    <ul className="records">
      {issues.map((issue) => (
        <li key={issue.issueKey}>
          <div className="record-row">
            <a className="record" href={paths.issue(issue.issueKey)}>
              <span className="key">{issue.issueKey}:</span> {issue.title}
            </a>
            <Status state={issue.state} />
          </div>
          {showGoal && (
            <a className="record-parent" href={paths.goal(issue.goalId)}>
              Goal: {issue.goalId}
            </a>
          )}
          {snapshot && !lookup(snapshot.issues, issue.issueKey) && (
            <NotAvailable label={`Issue ${issue.issueKey}`} />
          )}
        </li>
      ))}
    </ul>
  );
}
function IssueList({
  issues,
  snapshot,
  showGoal = true,
}: {
  issues: IssueSummary[];
  snapshot?: Snapshot;
  showGoal?: boolean;
}) {
  if (!issues.length) return <p>No issues here yet.</p>;
  const rest = issues.slice(listLimit);
  return (
    <>
      <IssueRows
        issues={issues.slice(0, listLimit)}
        snapshot={snapshot}
        showGoal={showGoal}
      />
      {rest.length > 0 && (
        <details className="more-records">
          <summary>
            Show {rest.length} more of {issues.length}
          </summary>
          <IssueRows issues={rest} snapshot={snapshot} showGoal={showGoal} />
        </details>
      )}
    </>
  );
}
/** Work that is moving comes before work that waits, so a reader sees what
 * the factory is doing now. The sort is stable: each group keeps its order. */
const stateRank: Partial<Record<State, number>> = {
  Working: 0,
  "In merge queue": 1,
};
export const activeFirst = (issues: IssueSummary[]): IssueSummary[] =>
  [...issues].sort(
    (a, b) => (stateRank[a.state] ?? 2) - (stateRank[b.state] ?? 2),
  );
const natural = new Intl.Collator("en", { numeric: true });
/** The floor: one square for each issue, in key order, marked by state.
 * The squares are a pointer shortcut, outside the tab order. A goal page
 * lists every issue as a link; the story page lists work in progress and
 * recent work, and links to each goal. */
function Floor({
  issues,
  anchors = false,
}: {
  issues: [string, Issue | null | undefined][];
  anchors?: boolean;
}) {
  if (!issues.length) return null;
  const sorted = [...issues].sort(([a], [b]) => natural.compare(a, b));
  const counts = states
    .map((state) => ({
      state,
      count: sorted.filter(([, issue]) => issue?.state === state).length,
    }))
    .filter(({ count }) => count > 0);
  return (
    <div className="floor">
      <div className="floor-grid" aria-hidden="true">
        {sorted.map(([key, issue]) => (
          <a
            key={key}
            href={paths.issue(key)}
            tabIndex={-1}
            data-state={issue?.state}
            title={issue ? `${key}: ${issue.title} (${issue.state})` : `${key}`}
          />
        ))}
      </div>
      <ul className="floor-key">
        {counts.map(({ state, count }) => (
          <li key={state} data-state={state}>
            {anchors ? (
              <a href={`#${stateId(state)}`}>
                <strong>{count}</strong> {state}
              </a>
            ) : (
              <span>
                <strong>{count}</strong> {state}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
function Progress({
  title,
  shipped,
  total,
}: {
  title: string;
  shipped: number;
  total: number;
}) {
  if (total <= 0) return <p>No issues yet.</p>;
  return (
    <div className="progress">
      <progress
        max={total}
        value={shipped}
        aria-label={`${title}: ${shipped} shipped of ${total} issues`}
      />
      <p className="progress-label">
        <strong>{Math.round((shipped / total) * 100)}%</strong> {shipped}{" "}
        shipped of {total} issues
      </p>
    </div>
  );
}

export function StoryPage({
  story,
  snapshot,
}: {
  story: Story;
  snapshot: Snapshot;
}) {
  const mergeHours = story.charts.issueToMergeHours.map(({ value }) => value);
  const reviewRounds = story.charts.reviewRounds.map(({ value }) => value);
  const manyGoals = story.goals.length > 1;
  // Count the same issues the floor draws, so the two always agree.
  const floor = Object.entries(snapshot.issues);
  const statement = shippedSentence(
    floor.filter(([, issue]) => issue?.state === "Shipped").length,
    floor.length,
  );
  return (
    <>
      <header className="lead story-lead">
        <Heading level={1}>{fillStats(story.headline, story.stats)}</Heading>
        {statement && <p className="statement">{statement}</p>}
        <Narrative paragraphs={story.narrative} stats={story.stats} />
        <Floor issues={floor} />
      </header>
      <section aria-labelledby="goals-heading">
        <SectionHead
          id="goals-heading"
          title={manyGoals ? "The goals" : "The goal"}
          count={manyGoals ? story.goals.length : undefined}
        />
        {!story.goals.length && <p>No goals published yet.</p>}
        <div className="chapters">
          {story.goals.map((goal) => (
            <article className="chapter" key={goal.goalId}>
              <div className="section-band">
                <h3>
                  <a href={paths.goal(goal.goalId)}>{goal.title}</a>
                </h3>
                <Status state={goal.state} />
              </div>
              <Narrative paragraphs={goal.chapter} stats={story.stats} />
              <Progress
                title={goal.title}
                shipped={goal.shipped}
                total={goal.total}
              />
              <a className="more" href={paths.goal(goal.goalId)}>
                Read the goal
              </a>
              {!lookup(snapshot.goals, goal.goalId) && (
                <NotAvailable label={`Goal ${goal.goalId}`} />
              )}
            </article>
          ))}
        </div>
      </section>
      <section aria-labelledby="numbers-heading">
        <SectionHead id="numbers-heading" title="The numbers" />
        <div className="measures">
          <DashboardChart
            formatValue={formatShort}
            formatLabel={axisLabel}
            title="Shipped per week"
            headline={weekSentence(story.charts.shippedPerWeek)}
            unit="issues"
            category="Week"
            points={story.charts.shippedPerWeek}
            slots={weekSlots}
          />
          <DashboardChart
            formatValue={formatShort}
            title="Issue-to-merge time"
            headline={mergeSentence(mergeHours)}
            note={countNote(mergeHours.length, "issue")}
            unit="issues"
            category="Time from issue to merge"
            points={tallyHours(mergeHours)}
          />
          <DashboardChart
            formatValue={formatShort}
            title="Review rounds per PR"
            headline={roundsSentence(reviewRounds)}
            note={countNote(reviewRounds.length, "PR")}
            unit="PRs"
            category="Review rounds"
            points={tallyCounts(reviewRounds)}
          />
          <DashboardChart
            formatValue={(value) => `${formatShort(value)}%`}
            formatLabel={axisLabel}
            title="First-pass review rate"
            headline={firstPassSentence(story.charts.firstPassReviewRate)}
            unit="%"
            category="Week"
            points={story.charts.firstPassReviewRate}
            maximum={100}
            slots={weekSlots}
          />
        </div>
      </section>
      <section aria-labelledby="working-heading">
        <SectionHead
          id="working-heading"
          title="Work in progress"
          count={story.workInProgress.length}
        />
        <IssueList
          issues={activeFirst(story.workInProgress)}
          snapshot={snapshot}
          showGoal={manyGoals}
        />
      </section>
      <section aria-labelledby="shipped-heading">
        <SectionHead
          id="shipped-heading"
          title="Recently shipped"
          count={story.recentlyShipped.length}
        />
        <IssueList
          issues={story.recentlyShipped}
          snapshot={snapshot}
          showGoal={manyGoals}
        />
      </section>
    </>
  );
}
const countNote = (count: number, noun: string) =>
  count ? `${count} ${noun}${count === 1 ? "" : "s"} measured` : undefined;

export function GoalPage({
  goal,
  snapshot,
}: {
  goal: Goal;
  snapshot: Snapshot;
}) {
  const issues = goal.issueKeys.map((key) => ({
    key,
    issue: lookup(snapshot.issues, key),
  }));
  const groups = states
    .map((state) => ({
      state,
      members: issues.flatMap(({ issue }) =>
        issue?.state === state
          ? [
              {
                issueKey: issue.key,
                goalId: issue.goalId,
                title: issue.title,
                state: issue.state,
              },
            ]
          : [],
      ),
    }))
    .filter(({ members }) => members.length > 0);
  return (
    <>
      <Trail />
      <header className="lead">
        <p className="eyebrow">Goal / {goal.id}</p>
        <Heading level={1}>{goal.title}</Heading>
        <Status state={goal.state} />
        <Narrative
          paragraphs={goal.chapter}
          stats={snapshot.story?.stats ?? {}}
        />
        {!snapshot.story && <NotAvailable label="Story statistics" />}
      </header>
      <BurnUp points={goal.burnUp} />
      <section aria-labelledby="issues-heading">
        <SectionHead
          id="issues-heading"
          title="Issues by state"
          count={issues.length}
        />
        {!issues.length && <p>No issues yet.</p>}
        <Floor anchors issues={issues.map(({ key, issue }) => [key, issue])} />
        {groups.map(({ state, members }) => (
          <section
            className="state-group"
            key={state}
            id={stateId(state)}
            tabIndex={-1}
          >
            <div className="section-band">
              <Heading level={3}>{state}</Heading>
              <p className="count">{members.length}</p>
            </div>
            <IssueList issues={members} showGoal={false} />
          </section>
        ))}
        {issues
          .filter(({ issue }) => !issue)
          .map(({ key }) => (
            <div key={key}>
              <a href={paths.issue(key)}>{key}</a>
              <NotAvailable label={`Issue ${key}`} />
            </div>
          ))}
      </section>
    </>
  );
}

const stateId = (state: State) =>
  `state-${state.toLowerCase().replaceAll(" ", "-")}`;

type SessionEvent = Extract<TimelineEvent, { kind: "session" }>;
type TimelineRow =
  | { kind: "event"; event: Exclude<TimelineEvent, SessionEvent> }
  | { kind: "session"; sessionId: string; events: SessionEvent[] };
/** Consecutive turns of one session read as one row. */
function timelineRows(timeline: TimelineEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const event of [...timeline].sort(
    (a, b) => Date.parse(a.at) - Date.parse(b.at),
  )) {
    const last = rows.at(-1);
    if (event.kind !== "session") rows.push({ kind: "event", event });
    else if (last?.kind === "session" && last.sessionId === event.sessionId)
      last.events.push(event);
    else
      rows.push({
        kind: "session",
        sessionId: event.sessionId,
        events: [event],
      });
  }
  return rows;
}

function EventDetail({
  event,
}: {
  event: Exclude<TimelineEvent, SessionEvent>;
}) {
  switch (event.kind) {
    case "opened":
      return <strong>Issue opened</strong>;
    case "pullRequest":
      return <a href={paths.pr(event.prNumber)}>PR #{event.prNumber} opened</a>;
    case "review":
      return (
        <>
          <a href={paths.pr(event.prNumber)}>PR #{event.prNumber}</a>
          <strong>
            Review round {event.round}: {event.outcome}
          </strong>
        </>
      );
    case "check":
      return (
        <strong>
          {event.name}: {event.result}
        </strong>
      );
    case "mergeQueue":
      return <strong>Merge queue: {event.state}</strong>;
    case "merged":
      return <a href={paths.pr(event.prNumber)}>PR #{event.prNumber} merged</a>;
    case "stopped":
      return <strong>Stopped</strong>;
  }
}
export function IssuePage({
  issue,
  snapshot,
}: {
  issue: Issue;
  snapshot: Snapshot;
}) {
  return (
    <>
      <Trail goalId={issue.goalId} />
      <header className="lead record-lead">
        <p className="eyebrow">Issue / {issue.key}</p>
        <Heading level={1}>{issue.title}</Heading>
        <Status state={issue.state} />
        <Text text={issue.description} />
      </header>
      <section aria-labelledby="timeline-heading">
        <Heading level={2} id="timeline-heading">
          From issue to merge
        </Heading>
        {!issue.timeline.length && <p>No events published yet.</p>}
        <ol className="timeline">
          {timelineRows(issue.timeline).map((row, index) =>
            row.kind === "event" ? (
              <li key={index} data-kind={row.event.kind}>
                <time dateTime={row.event.at}>
                  {formatInstant(row.event.at)}
                </time>
                <div className="event">
                  <div className="event-heading">
                    <EventDetail event={row.event} />
                  </div>
                  {/* The exporter's default text repeats the heading. */}
                  {!(
                    row.event.kind === "opened" &&
                    row.event.text === "Issue opened"
                  ) && <p>{row.event.text}</p>}
                </div>
              </li>
            ) : (
              <li key={index} data-kind="session">
                <time dateTime={row.events[0]!.at}>
                  {formatInstant(row.events[0]!.at)}
                </time>
                <div className="event">
                  <div className="event-heading">
                    <a href={paths.session(row.sessionId)}>
                      Session {sessionLabel(row.sessionId)}
                    </a>
                    <span>
                      {row.events.length}{" "}
                      {row.events.length === 1 ? "turn" : "turns"}
                    </span>
                  </div>
                  <div className="turns">
                    {row.events.map((event) => (
                      <a
                        key={event.turn}
                        href={`${paths.session(row.sessionId)}#turn-${event.turn}`}
                        aria-label={`Session ${sessionLabel(row.sessionId)}, turn ${event.turn}`}
                        title={event.text}
                      >
                        {event.turn}
                      </a>
                    ))}
                  </div>
                  {!lookup(snapshot.sessions, row.sessionId) && (
                    <NotAvailable label={`Session ${row.sessionId}`} />
                  )}
                </div>
              </li>
            ),
          )}
        </ol>
      </section>
    </>
  );
}

export function TranscriptTurn({ turn }: { turn: Turn }) {
  return (
    <article
      className="turn"
      id={`turn-${turn.number}`}
      tabIndex={-1}
      data-role={turn.role}
    >
      <div className="turn-head">
        <h2>
          Turn {turn.number} / {turn.role}
        </h2>
        <time dateTime={turn.at}>{formatInstant(turn.at)}</time>
      </div>
      <div className="turn-body">
        {turn.withheld ? (
          <p className="withheld">{withheldMessage}</p>
        ) : (
          <>
            <Text text={turn.text} />
            {turn.toolCalls.map((tool, index) => (
              <details className="tool" key={index}>
                <summary>Tool call: {tool.name}</summary>
                <h3>Input</h3>
                <pre>{tool.input}</pre>
                <h3>Output</h3>
                <pre>{tool.output}</pre>
              </details>
            ))}
            {turn.diff !== undefined && (
              <section
                className="diff"
                aria-label={`Diff for turn ${turn.number}`}
              >
                <h3>Diff</h3>
                <pre>
                  {turn.diff.split("\n").map((line, index) => (
                    <span
                      key={index}
                      className={
                        line.startsWith("+")
                          ? "addition"
                          : line.startsWith("-")
                            ? "deletion"
                            : undefined
                      }
                    >
                      {line}
                      {"\n"}
                    </span>
                  ))}
                </pre>
              </section>
            )}
          </>
        )}
      </div>
    </article>
  );
}
export function SessionPage({ session }: { session: Session }) {
  return (
    <>
      <Trail goalId={session.goalId} issueKey={session.issueKey} />
      <header className="lead record-lead">
        <p className="eyebrow">Session / {sessionLabel(session.id)}</p>
        <Heading level={1}>{session.title}</Heading>
        <Status state={session.state} />
        <p>{session.turns.length} turns. Tool calls open on request.</p>
      </header>
      <nav className="jump" aria-label="Transcript">
        <a href="#transcript">Start of transcript</a>
        <a href="#transcript-end">End of transcript</a>
      </nav>
      <div id="transcript" tabIndex={-1}>
        {session.turns.map((turn) => (
          <TranscriptTurn key={turn.number} turn={turn} />
        ))}
        {!session.turns.length && <p>No turns published yet.</p>}
      </div>
      <p id="transcript-end" tabIndex={-1}>
        <a href={paths.issue(session.issueKey)}>Back to {session.issueKey}</a>
      </p>
    </>
  );
}

export function Pages({
  snapshot,
  path,
}: {
  snapshot: Snapshot;
  path: string;
}) {
  if (/^\/factory\/?$/.test(path))
    return snapshot.story ? (
      <StoryPage story={snapshot.story} snapshot={snapshot} />
    ) : (
      <>
        <Heading level={1}>Factory story</Heading>
        <NotAvailable label="Story" />
        <section aria-labelledby="goals-heading">
          <Heading level={2} id="goals-heading">
            The goals
          </Heading>
          {Object.entries(snapshot.goals).map(([id, goal]) => (
            <article className="chapter" key={id}>
              <h3>
                <a href={paths.goal(id)}>{goal?.title ?? id}</a>
              </h3>
              {goal ? (
                <Narrative paragraphs={goal.chapter} stats={{}} />
              ) : (
                <NotAvailable label={`Goal ${id}`} />
              )}
            </article>
          ))}
        </section>
      </>
    );
  const match = /^\/factory\/(goals|issues|sessions)\/([^/]+)\/?$/.exec(path);
  let id = "";
  try {
    id = decodeURIComponent(match?.[2] ?? "");
  } catch {
    /* Invalid routes show the same unavailable state. */
  }
  if (match?.[1] === "goals") {
    const goal = lookup(snapshot.goals, id);
    if (goal) return <GoalPage goal={goal} snapshot={snapshot} />;
  } else if (match?.[1] === "issues") {
    const issue = lookup(snapshot.issues, id);
    if (issue) return <IssuePage issue={issue} snapshot={snapshot} />;
  } else if (match?.[1] === "sessions") {
    const session = lookup(snapshot.sessions, id);
    if (session) return <SessionPage session={session} />;
  }
  return (
    <>
      <Trail />
      <Heading level={1}>Record not available</Heading>
      <p>The snapshot has no available record for {path}.</p>
    </>
  );
}
