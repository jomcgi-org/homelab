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
import { fillStats, formatNumber, paths } from "./data";
import { DashboardChart } from "../DashboardChart";
import { BurnUp } from "./Charts";

const storyBarWidth = 32;

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
function IssueList({
  issues,
  snapshot,
}: {
  issues: IssueSummary[];
  snapshot?: Snapshot;
}) {
  if (!issues.length) return <p>No issues here yet.</p>;
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
          <a className="record-parent" href={paths.goal(issue.goalId)}>
            Goal: {issue.goalId}
          </a>
          {snapshot && !lookup(snapshot.issues, issue.issueKey) && (
            <NotAvailable label={`Issue ${issue.issueKey}`} />
          )}
        </li>
      ))}
    </ul>
  );
}

export function StoryPage({
  story,
  snapshot,
}: {
  story: Story;
  snapshot: Snapshot;
}) {
  return (
    <>
      <header className="lead">
        <p className="eyebrow">Homelab / Factory</p>
        <Heading level={1}>{fillStats(story.headline, story.stats)}</Heading>
        <Narrative paragraphs={story.narrative} stats={story.stats} />
      </header>
      <section aria-labelledby="goals-heading">
        <Heading level={2} id="goals-heading">
          The goals
        </Heading>
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
              <div className="progress">
                {goal.total > 0 ? (
                  <progress
                    max={goal.total}
                    value={goal.shipped}
                    aria-label={`${goal.title}: ${goal.shipped} shipped of ${goal.total} issues`}
                  />
                ) : (
                  <p>No issues yet.</p>
                )}
                <p className="progress-label">
                  {goal.shipped} shipped of {goal.total} issues
                </p>
              </div>
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
        <Heading level={2} id="numbers-heading">
          The numbers
        </Heading>
        <dl className="stats">
          {Object.entries(story.stats).map(([key, stat]) => (
            <div key={key}>
              <dt>{stat.label}</dt>
              <dd>
                {formatNumber(stat.value)} <span>{stat.unit}</span>
              </dd>
            </div>
          ))}
        </dl>
        <div className="charts">
          <DashboardChart
            formatValue={formatNumber}
            title="Shipped per week"
            unit="issues"
            points={story.charts.shippedPerWeek}
            maximumBarWidth={storyBarWidth}
          />
          <DashboardChart
            formatValue={formatNumber}
            title="Issue-to-merge time"
            unit="hours"
            points={story.charts.issueToMergeHours}
            maximumBarWidth={storyBarWidth}
          />
          <DashboardChart
            formatValue={formatNumber}
            title="Review rounds per PR"
            unit="rounds"
            points={story.charts.reviewRounds}
            maximumBarWidth={storyBarWidth}
          />
          <DashboardChart
            formatValue={formatNumber}
            title="First-pass review rate"
            unit="%"
            points={story.charts.firstPassReviewRate}
            maximum={100}
            maximumBarWidth={storyBarWidth}
          />
        </div>
      </section>
      <section aria-labelledby="working-heading">
        <Heading level={2} id="working-heading">
          Work in progress
        </Heading>
        <IssueList issues={story.workInProgress} snapshot={snapshot} />
      </section>
      <section aria-labelledby="shipped-heading">
        <Heading level={2} id="shipped-heading">
          Recently shipped
        </Heading>
        <IssueList issues={story.recentlyShipped} snapshot={snapshot} />
      </section>
    </>
  );
}

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
        <Heading level={2} id="issues-heading">
          Issues by state
        </Heading>
        {!issues.length && <p>No issues yet.</p>}
        {states.map((state) => {
          const members = issues.flatMap(({ issue }) =>
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
          );
          return (
            members.length > 0 && (
              <section className="state-group" key={state}>
                <Heading level={3}>{state}</Heading>
                <IssueList issues={members} />
              </section>
            )
          );
        })}
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

function EventDetail({ event }: { event: TimelineEvent }) {
  switch (event.kind) {
    case "opened":
      return <strong>Issue opened</strong>;
    case "session":
      return (
        <a href={`${paths.session(event.sessionId)}#turn-${event.turn}`}>
          Session {event.sessionId}, turn {event.turn}
        </a>
      );
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
      <header className="lead">
        <p className="eyebrow">Issue / {issue.key}</p>
        <Heading level={1}>{issue.title}</Heading>
        <Status state={issue.state} />
        <p>{issue.description}</p>
      </header>
      <section aria-labelledby="timeline-heading">
        <Heading level={2} id="timeline-heading">
          From issue to merge
        </Heading>
        {!issue.timeline.length && <p>No events published yet.</p>}
        <ol className="timeline">
          {[...issue.timeline]
            .sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
            .map((event, index) => (
              <li key={index} data-kind={event.kind}>
                <time dateTime={event.at}>{displayTime(event.at)}</time>
                <div className="event">
                  <div className="event-heading">
                    <EventDetail event={event} />
                  </div>
                  <p>{event.text}</p>
                  {event.kind === "session" &&
                    !lookup(snapshot.sessions, event.sessionId) && (
                      <NotAvailable label={`Session ${event.sessionId}`} />
                    )}
                </div>
              </li>
            ))}
        </ol>
      </section>
    </>
  );
}
function displayTime(value: string) {
  return value.replace("T", " ").replace(/(?:\.\d+)?Z$/, " UTC");
}

export function TranscriptTurn({ turn }: { turn: Turn }) {
  return (
    <article className="turn" id={`turn-${turn.number}`} tabIndex={-1}>
      <div className="section-band">
        <h2>
          Turn {turn.number} / {turn.role}
        </h2>
        <time dateTime={turn.at}>{displayTime(turn.at)}</time>
      </div>
      {turn.withheld ? (
        <p className="withheld">{withheldMessage}</p>
      ) : (
        <>
          <p className="transcript-text">{turn.text}</p>
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
    </article>
  );
}
export function SessionPage({ session }: { session: Session }) {
  return (
    <>
      <Trail goalId={session.goalId} issueKey={session.issueKey} />
      <header className="lead">
        <p className="eyebrow">Session / {session.id}</p>
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
