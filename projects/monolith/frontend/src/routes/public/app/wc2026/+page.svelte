<script>
  let { data } = $props();

  const focus = $derived(data.focus ?? "SCO");
  const groupName = $derived(data.group ?? "");
  const table = $derived(data.group_table ?? []);
  const q = $derived(data.qualification ?? {});
  const swings = $derived(data.swing_matches ?? []);

  // Probabilities arrive as raw 0..1 floats; the page is the only place they
  // become percentages.
  const pct = (x) => `${Math.round((x ?? 0) * 100)}%`;
  const points = (x) => Math.round((x ?? 0) * 100);

  // The headline reads the status first: a settled group shows the verdict,
  // otherwise the live qualify probability.
  const headline = $derived(
    q.status === "qualified"
      ? "Qualified"
      : q.status === "eliminated"
        ? "Eliminated"
        : pct(q.prob_qualify),
  );

  const MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // Kickoffs render in UTC so the screenshot is identical on every runner and
  // viewers always see the same canonical match time.
  function fmtKick(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${hh}:${mm} UTC`;
  }

  function fmtUpdated(iso) {
    if (!iso) return "unknown";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "unknown";
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${hh}:${mm} UTC`;
  }

  const fmtGd = (gd) => (gd > 0 ? `+${gd}` : `${gd}`);
</script>

<svelte:head>
  <title>Scotland at the 2026 World Cup</title>
  <meta
    name="description"
    content="Scotland's chance of reaching the 2026 World Cup Round of 32, and the remaining matches that most change it. Elo-weighted Monte Carlo simulation."
  />
  <meta name="robots" content="noindex" />
</svelte:head>

<div class="page">
  <div class="board">
    <header class="board-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="https://jomcgi.dev/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">wc2026</span>
        </nav>
        {#if groupName}
          <p class="stats"><strong>Group {groupName}</strong></p>
        {/if}
      </div>
      <h1 class="title">Scotland at the 2026 World Cup</h1>
      <p class="source">
        Scotland's chance of reaching the Round of 32, and the remaining matches
        that most change it.
      </p>
    </header>

    <!-- HEADLINE -->
    <section class="headline">
      <p class="headline-label">Scotland's chance of reaching the Round of 32</p>
      <p class="headline-figure" class:verdict={q.status === "qualified" || q.status === "eliminated"}>
        {headline}
      </p>

      <div class="routes">
        <div class="route">
          <span class="route-num">{pct(q.prob_top2)}</span>
          <span class="route-text">As group winner or runner-up</span>
        </div>
        <div class="route">
          <span class="route-num">{pct(q.prob_third)}</span>
          <span class="route-text">As one of the 8 best third-placed teams</span>
        </div>
      </div>
      <p class="route-note">
        The top-two route is the long shot: most of Scotland's path runs through
        the best third-placed places, not a podium finish in the group.
      </p>
    </section>

    <!-- GROUP TABLE -->
    <section class="block">
      <h2 class="block-title">Group {groupName} table</h2>
      <div class="table-scroll">
        <table class="grid">
          <thead>
            <tr>
              <th class="col-team" scope="col">Team</th>
              <th scope="col">P</th>
              <th scope="col">W</th>
              <th scope="col">D</th>
              <th scope="col">L</th>
              <th scope="col">GF</th>
              <th scope="col">GA</th>
              <th scope="col">GD</th>
              <th class="col-pts" scope="col">Pts</th>
            </tr>
          </thead>
          <tbody>
            {#each table as t (t.team_id)}
              <tr class:focus={t.fifa_code === focus}>
                <td class="col-team">
                  <span class="team">
                    {#if t.flag_url}
                      <img class="flag" src={t.flag_url} alt="" width="22" height="15" loading="lazy" />
                    {/if}
                    <span class="team-name">{t.name}</span>
                  </span>
                </td>
                <td>{t.mp}</td>
                <td>{t.w}</td>
                <td>{t.d}</td>
                <td>{t.l}</td>
                <td>{t.gf}</td>
                <td>{t.ga}</td>
                <td>{fmtGd(t.gd)}</td>
                <td class="col-pts">{t.pts}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- SWING MATCHES -->
    <section class="block">
      <h2 class="block-title">Matches that could change it</h2>
      <p class="block-sub">
        Each remaining match, ranked by how much its result moves Scotland's
        qualify chance. The three figures are Scotland's qualify chance after
        each outcome.
      </p>

      {#if swings.length === 0}
        <p class="empty">No remaining matches to model right now.</p>
      {:else}
        <ul class="swings">
          {#each swings as m (m.match_id)}
            <li class="swing">
              <div class="swing-head">
                <span class="fixture">
                  {m.home_code} <span class="v">v</span> {m.away_code}
                  {#if m.is_own_match}
                    <span class="badge own">Scotland</span>
                  {/if}
                  <span class="badge grp">Group {m.group_name}</span>
                </span>
                <span class="swing-mag" title="Swing magnitude">
                  &plusmn;{points(m.swing)} pts
                </span>
              </div>
              <p class="kickoff">{fmtKick(m.kickoff)}</p>
              <div class="outcomes">
                <span class="out">
                  <span class="out-label">If {m.home_code} win</span>
                  <span class="out-num">{pct(m.p_qualify_home_win)}</span>
                </span>
                <span class="out">
                  <span class="out-label">Draw</span>
                  <span class="out-num">{pct(m.p_qualify_draw)}</span>
                </span>
                <span class="out">
                  <span class="out-label">If {m.away_code} win</span>
                  <span class="out-num">{pct(m.p_qualify_away_win)}</span>
                </span>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <!-- FOOTER -->
    <footer class="foot">
      <p>
        Data from <a href="https://worldcup26.ir" rel="external noopener">worldcup26.ir</a>.
        Odds from an Elo-weighted Monte Carlo, {q.n_sims ?? 0} simulations.
      </p>
      <p class="caveat">
        The final two FIFA tiebreakers (disciplinary record and FIFA ranking)
        are not modelled and are treated as coin-flips, so tight ties carry a
        little extra noise.
      </p>
      <p class="updated">Last updated {fmtUpdated(data.updated_at)}.</p>
    </footer>
  </div>
</div>

<style>
  .page {
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
    padding: 16px 12px;
  }

  .board {
    max-width: 680px;
    margin: 0 auto;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .board-head {
    display: flex;
    flex-direction: column;
    gap: 9px;
    padding: 12px 14px;
    border-bottom: 2px solid var(--ink);
  }

  .crumb-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 14px;
    flex-wrap: wrap;
  }

  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
    padding: 0 2px;
  }

  .crumb-arrow {
    font-size: 0.85em;
  }

  .stats {
    margin: 0;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .stats strong {
    color: var(--ink);
    font-weight: 700;
  }

  .title {
    margin: 0;
    font-family: var(--serif);
    font-weight: 400;
    letter-spacing: -0.02em;
    line-height: 1;
    font-size: 30px;
  }

  .source {
    margin: 0;
    font-size: 13px;
    line-height: 1.4;
    color: var(--ink-3);
  }

  /* ── Headline figure ─────────────────────── */
  .headline {
    padding: 18px 14px;
    border-bottom: 2px solid var(--ink);
    text-align: center;
  }

  .headline-label {
    margin: 0;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .headline-figure {
    margin: 4px 0 2px;
    font-family: var(--serif);
    font-weight: 400;
    line-height: 0.95;
    font-size: 88px;
    display: inline-block;
    padding: 0 10px;
    background: linear-gradient(transparent 64%, var(--accent) 64%);
  }

  .headline-figure.verdict {
    font-size: 56px;
  }

  .routes {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 14px;
  }

  .route {
    flex: 1 1 200px;
    max-width: 280px;
    border: 2px solid var(--ink);
    padding: 10px 12px;
    text-align: left;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }

  .route-num {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 22px;
    white-space: nowrap;
  }

  .route-text {
    font-size: 13px;
    line-height: 1.25;
    color: var(--ink-3);
  }

  .route-note {
    margin: 12px auto 0;
    max-width: 480px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink-3);
  }

  /* ── Generic block ───────────────────────── */
  .block {
    padding: 14px;
    border-bottom: 2px solid var(--ink);
  }

  .block-title {
    margin: 0 0 4px;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .block-sub {
    margin: 0 0 12px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink-3);
  }

  /* ── Group table ─────────────────────────── */
  .table-scroll {
    overflow-x: auto;
  }

  .grid {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 13px;
  }

  .grid th {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--ink-3);
    text-align: right;
    padding: 6px 6px;
    border-bottom: 2px solid var(--ink);
    white-space: nowrap;
  }

  .grid td {
    text-align: right;
    padding: 8px 6px;
    border-bottom: 1px solid var(--rule);
    white-space: nowrap;
  }

  .grid tbody tr:last-child td {
    border-bottom: none;
  }

  .col-team {
    text-align: left !important;
    width: 100%;
  }

  .col-pts {
    font-weight: 700;
  }

  .grid th.col-pts {
    color: var(--ink);
  }

  .team {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .flag {
    border: 1px solid var(--ink);
    flex-shrink: 0;
  }

  .team-name {
    font-family: var(--sans);
    font-weight: 600;
    font-size: 14px;
  }

  .grid tbody tr.focus {
    background: var(--accent);
  }

  .grid tbody tr.focus td {
    border-bottom-color: var(--ink);
  }

  /* ── Swing matches ───────────────────────── */
  .swings {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .swing {
    border: 2px solid var(--ink);
    padding: 10px 12px;
  }

  .swing-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  .fixture {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 16px;
    display: inline-flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 7px;
  }

  .fixture .v {
    color: var(--ink-3);
    font-weight: 600;
  }

  .swing-mag {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.02em;
    white-space: nowrap;
  }

  .kickoff {
    margin: 4px 0 10px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  .outcomes {
    display: flex;
    gap: 8px;
  }

  .out {
    flex: 1 1 0;
    border: 1.5px solid var(--ink);
    padding: 7px 6px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: center;
    min-width: 0;
  }

  .out-label {
    font-size: 11px;
    line-height: 1.2;
    color: var(--ink-3);
  }

  .out-num {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 18px;
  }

  .badge {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 1px 7px;
    border: 1.5px solid var(--ink);
    border-radius: 999px;
    white-space: nowrap;
    align-self: center;
  }

  .badge.own {
    background: var(--accent);
  }

  .badge.grp {
    background: transparent;
    color: var(--ink-3);
  }

  /* ── Footer ──────────────────────────────── */
  .foot {
    padding: 14px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink-3);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .foot a {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
  }

  .foot .updated {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
  }

  .empty {
    margin: 0;
    font-size: 13px;
    color: var(--ink-3);
  }

  /* ── Desktop scale-up ────────────────────── */
  @media (min-width: 768px) {
    .page {
      padding: 32px 24px;
    }
    .board {
      max-width: 820px;
    }
    .board-head {
      gap: 11px;
      padding: 18px 24px;
    }
    .title {
      font-size: 40px;
    }
    .source {
      font-size: 14px;
    }
    .headline {
      padding: 28px 24px;
    }
    .headline-figure {
      font-size: 120px;
    }
    .headline-figure.verdict {
      font-size: 72px;
    }
    .block {
      padding: 20px 24px;
    }
    .grid {
      font-size: 14px;
    }
    .foot {
      padding: 20px 24px;
    }
  }

  @media (max-width: 420px) {
    .outcomes {
      flex-direction: column;
    }
    .out {
      flex-direction: row;
      justify-content: space-between;
      align-items: center;
      text-align: left;
    }
  }
</style>
