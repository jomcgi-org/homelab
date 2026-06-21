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

  const pctNum = (x) => Math.round((x ?? 0) * 100);

  // The current (unconditional) qualify chance. The swing cards plot each
  // outcome relative to this "Now" point.
  const baselinePct = $derived(pctNum(q.prob_qualify));

  // Hero outcome split. Top-two and best-third are the two qualifying routes;
  // the remainder is elimination. Integer percents that always sum to 100.
  const top2Pct = $derived(pctNum(q.prob_top2));
  const thirdPct = $derived(pctNum(q.prob_third));
  const elimPct = $derived(Math.max(0, 100 - top2Pct - thirdPct));

  // Shared number-line domain for the swing cards: a tidy lower bound (rounded
  // down to a 10) so every match sits on the same comparable scale, with 100%
  // as the upper bound.
  const _allCond = $derived(
    swings.flatMap((m) => [
      pctNum(m.p_qualify_home_win),
      pctNum(m.p_qualify_draw),
      pctNum(m.p_qualify_away_win),
    ]),
  );
  const lineLo = $derived(
    swings.length
      ? Math.max(0, Math.floor(Math.min(baselinePct, ..._allCond) / 10) * 10)
      : 0,
  );
  const linePos = (x) => {
    const span = 100 - lineLo;
    return span <= 0 ? 0 : ((pctNum(x) - lineLo) / span) * 100;
  };

  // Swing magnitude bar, scaled to the biggest mover so the cards are
  // comparable at a glance.
  const maxSwing = $derived(
    swings.length ? Math.max(...swings.map((m) => m.swing ?? 0)) : 1,
  );
  const swingBar = (s) => (maxSwing > 0 ? ((s ?? 0) / maxSwing) * 100 : 0);

  // Each outcome's shift versus the current qualify chance, in points.
  const delta = (x) => pctNum(x) - baselinePct;
  const UP_TRI = "▲";
  const DOWN_TRI = "▼";
  const deltaArrow = (x) => {
    const d = delta(x);
    return d > 0 ? UP_TRI : d < 0 ? DOWN_TRI : "";
  };
  const deltaAbs = (x) => Math.abs(delta(x));
  const deltaClass = (x) => {
    const d = delta(x);
    return d > 0 ? "up" : d < 0 ? "down" : "flat";
  };
  const bestCond = (m) =>
    Math.max(
      pctNum(m.p_qualify_home_win),
      pctNum(m.p_qualify_draw),
      pctNum(m.p_qualify_away_win),
    );
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

      <div
        class="outcome-bar"
        role="img"
        aria-label="Top-two finish {top2Pct}%, best third {thirdPct}%, eliminated {elimPct}%"
      >
        {#if top2Pct > 0}
          <div class="seg seg-top2" style="width:{top2Pct}%">
            <span class="seg-num">{top2Pct}%</span>
          </div>
        {/if}
        {#if thirdPct > 0}
          <div class="seg seg-third" style="width:{thirdPct}%">
            <span class="seg-num">{thirdPct}%</span>
            <span class="seg-label">Best third-placed route</span>
          </div>
        {/if}
        {#if elimPct > 0}
          <div class="seg seg-elim" style="width:{elimPct}%">
            <span class="seg-num">{elimPct}%</span>
          </div>
        {/if}
      </div>

      <ul class="outcome-legend">
        <li>
          <span class="key key-top2"></span>Top-two finish
          <strong>{top2Pct}%</strong>
        </li>
        <li>
          <span class="key key-third"></span>Best third
          <strong>{thirdPct}%</strong>
        </li>
        <li>
          <span class="key key-elim"></span>Eliminated
          <strong>{elimPct}%</strong>
        </li>
      </ul>

      <p class="route-note">
        Almost all of Scotland's path runs through the best third-placed places,
        the group top-two route is the long shot.
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
          {#each swings as m, i (m.match_id)}
            {@const ph = linePos(m.p_qualify_home_win)}
            {@const pd = linePos(m.p_qualify_draw)}
            {@const pa = linePos(m.p_qualify_away_win)}
            {@const lo = Math.min(ph, pd, pa)}
            {@const hi = Math.max(ph, pd, pa)}
            {@const best = bestCond(m)}
            <li class="swing">
              <div class="swing-head">
                <span class="fixture">
                  <span class="rank">{String(i + 1).padStart(2, "0")}</span>
                  {m.home_code} <span class="v">v</span> {m.away_code}
                  {#if m.is_own_match}
                    <span class="badge own">Scotland</span>
                  {/if}
                  <span class="badge grp">Group {m.group_name}</span>
                </span>
                <span
                  class="swing-mag"
                  title="How much this match moves Scotland's chance"
                >
                  <span class="swing-track">
                    <span
                      class="swing-fill"
                      style="width:{swingBar(m.swing)}%"
                    ></span>
                  </span>
                  &plusmn;{points(m.swing)}
                </span>
              </div>
              <p class="kickoff">{fmtKick(m.kickoff)}</p>

              <div class="line">
                <span class="line-track">
                  <span
                    class="line-span"
                    style="left:{lo}%; right:{100 - hi}%"
                  ></span>
                  <span class="dot dot-lo" style="left:{lo}%"></span>
                  <span class="dot dot-hi" style="left:{hi}%"></span>
                  <span class="now" style="left:{linePos(q.prob_qualify)}%">
                    <span class="now-tick"></span>
                    <span class="now-label">Now</span>
                  </span>
                </span>
                <span class="line-lo">{lineLo}%</span>
                <span class="line-hi">100%</span>
              </div>

              <div class="outcomes">
                <span
                  class="out {deltaClass(m.p_qualify_home_win)}"
                  class:best={pctNum(m.p_qualify_home_win) === best}
                >
                  <span class="out-label">If {m.home_code} win</span>
                  <span class="out-num">{pct(m.p_qualify_home_win)}</span>
                  {#if deltaAbs(m.p_qualify_home_win) > 0}
                    <span class="out-delta"
                      >{deltaArrow(m.p_qualify_home_win)}{deltaAbs(
                        m.p_qualify_home_win,
                      )}</span
                    >
                  {/if}
                </span>
                <span
                  class="out {deltaClass(m.p_qualify_draw)}"
                  class:best={pctNum(m.p_qualify_draw) === best}
                >
                  <span class="out-label">Draw</span>
                  <span class="out-num">{pct(m.p_qualify_draw)}</span>
                  {#if deltaAbs(m.p_qualify_draw) > 0}
                    <span class="out-delta"
                      >{deltaArrow(m.p_qualify_draw)}{deltaAbs(
                        m.p_qualify_draw,
                      )}</span
                    >
                  {/if}
                </span>
                <span
                  class="out {deltaClass(m.p_qualify_away_win)}"
                  class:best={pctNum(m.p_qualify_away_win) === best}
                >
                  <span class="out-label">If {m.away_code} win</span>
                  <span class="out-num">{pct(m.p_qualify_away_win)}</span>
                  {#if deltaAbs(m.p_qualify_away_win) > 0}
                    <span class="out-delta"
                      >{deltaArrow(m.p_qualify_away_win)}{deltaAbs(
                        m.p_qualify_away_win,
                      )}</span
                    >
                  {/if}
                </span>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <!-- HOW IT WORKS (expandable) -->
    <details class="explainer">
      <summary class="explainer-summary">
        <span>How does this work?</span>
        <span class="explainer-toggle" aria-hidden="true"></span>
      </summary>
      <div class="explainer-body">
        <p>
          The percentage is not a bookmaker's price or a gut feel. It comes from
          replaying the rest of the group stage thousands of times and counting
          how often Scotland comes through.
        </p>
        <ol class="explainer-steps">
          <li>
            <strong>Rate every team.</strong> Each side carries an Elo rating
            (Scotland's is mid-table), so a bigger gap means a bigger favourite.
            Ratings are fixed at the tournament's start.
          </li>
          <li>
            <strong>Play out each remaining match.</strong> For every game still
            to come, the simulation rolls a realistic scoreline, the stronger
            team more likely to score more. Evenly matched sides draw about a
            quarter of the time, just like real football.
          </li>
          <li>
            <strong>Apply the World Cup rules.</strong> The top two of every
            group go through, plus the eight best third-placed teams, ranked by
            points, then goal difference, then goals scored. Scotland's likeliest
            route runs through those best-third places.
          </li>
          <li>
            <strong>Do it 20,000 times.</strong> Each run gives a different set
            of results. Scotland's chance is simply how many of those runs end
            with them in the Round of 32.
          </li>
          <li>
            <strong>Find what matters.</strong> "Matches that could change it"
            compares Scotland's chance across each possible result of every
            remaining game, then ranks them by how far the needle moves. That is
            how games in other groups show up here: they decide who Scotland is
            racing for a third-place spot.
          </li>
        </ol>
        <p class="explainer-fine">
          It is a model, not a crystal ball: matches are treated as independent,
          ratings do not move during the tournament, and the last two FIFA
          tiebreakers (disciplinary record and world ranking) are left as
          coin-flips. The numbers shift as real results land.
        </p>
      </div>
    </details>

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

  /* Outcome bar: top-two / best-third / eliminated, widths sum to 100. */
  .outcome-bar {
    display: flex;
    margin-top: 16px;
    height: 40px;
    border: 2px solid var(--ink);
    overflow: hidden;
  }

  .seg {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    font-family: var(--mono);
    font-weight: 700;
  }

  .seg + .seg {
    border-left: 2px solid var(--ink);
  }

  .seg-top2 {
    background: var(--ink);
    color: var(--paper);
  }

  .seg-third {
    background: var(--accent);
    color: var(--ink);
  }

  .seg-elim {
    background: var(--rule);
    color: var(--ink-3);
  }

  .seg-num {
    font-size: 13px;
  }

  .seg-label {
    display: none;
    font-size: 11px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .outcome-legend {
    list-style: none;
    margin: 10px 0 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 6px 16px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .outcome-legend li {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .outcome-legend strong {
    color: var(--ink);
  }

  .key {
    width: 11px;
    height: 11px;
    border: 1.5px solid var(--ink);
    flex-shrink: 0;
  }

  .key-top2 {
    background: var(--ink);
  }

  .key-third {
    background: var(--accent);
  }

  .key-elim {
    background: var(--rule);
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

  .rank {
    color: var(--ink-3);
    font-weight: 700;
  }

  .swing-mag {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.02em;
    white-space: nowrap;
  }

  .swing-track {
    width: 46px;
    height: 8px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
  }

  .swing-fill {
    display: block;
    height: 100%;
    background: var(--ink);
  }

  .kickoff {
    margin: 4px 0 6px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  /* Number line: each outcome's qualify chance on a shared axis, "Now" the
     current baseline, with the worst (orange) and best (yellow) outcomes
     marking the range. */
  .line {
    position: relative;
    padding: 20px 8px 16px;
  }

  .line-track {
    display: block;
    position: relative;
    height: 4px;
    background: var(--rule);
  }

  .line-span {
    position: absolute;
    top: 0;
    height: 100%;
    background: var(--ink);
  }

  .dot {
    position: absolute;
    top: 50%;
    width: 12px;
    height: 12px;
    border: 2px solid var(--ink);
    border-radius: 50%;
    transform: translate(-50%, -50%);
  }

  .dot-lo {
    background: var(--coral);
  }

  .dot-hi {
    background: var(--accent);
  }

  .now {
    position: absolute;
    top: 50%;
  }

  .now-tick {
    position: absolute;
    left: 0;
    top: -8px;
    width: 2px;
    height: 16px;
    background: var(--ink-3);
    transform: translateX(-50%);
  }

  .now-label {
    position: absolute;
    left: 0;
    bottom: 10px;
    transform: translateX(-50%);
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .line-lo,
  .line-hi {
    position: absolute;
    bottom: 0;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
  }

  .line-lo {
    left: 0;
  }

  .line-hi {
    right: 0;
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

  .out.best {
    background: var(--accent);
    border-color: var(--ink);
  }

  .out-delta {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.02em;
  }

  .out.up .out-delta {
    color: var(--ink);
  }

  .out.down .out-delta {
    color: var(--coral);
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

  /* ── How it works (expandable) ───────────── */
  .explainer {
    border-bottom: 2px solid var(--ink);
  }

  .explainer-summary {
    list-style: none;
    cursor: pointer;
    padding: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .explainer-summary::-webkit-details-marker {
    display: none;
  }

  .explainer[open] .explainer-summary {
    background: var(--accent);
  }

  /* Custom plus/minus toggle (no JS, reflects the native open state). */
  .explainer-toggle {
    position: relative;
    width: 16px;
    height: 16px;
    flex-shrink: 0;
    border: 2px solid var(--ink);
  }

  .explainer-toggle::before,
  .explainer-toggle::after {
    content: "";
    position: absolute;
    background: var(--ink);
  }

  .explainer-toggle::before {
    top: 50%;
    left: 3px;
    right: 3px;
    height: 2px;
    transform: translateY(-50%);
  }

  .explainer-toggle::after {
    left: 50%;
    top: 3px;
    bottom: 3px;
    width: 2px;
    transform: translateX(-50%);
  }

  .explainer[open] .explainer-toggle::after {
    display: none;
  }

  .explainer-body {
    padding: 0 14px 16px;
    font-size: 13px;
    line-height: 1.55;
    color: var(--ink-3);
  }

  .explainer-body > p {
    margin: 0 0 10px;
  }

  .explainer-steps {
    margin: 0 0 10px;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .explainer-steps strong {
    color: var(--ink);
  }

  .explainer-fine {
    margin: 0;
    font-size: 12px;
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
    .outcome-bar {
      height: 46px;
    }
    .seg-label {
      display: inline;
    }
    .block {
      padding: 20px 24px;
    }
    .grid {
      font-size: 14px;
    }
    .explainer-summary {
      padding: 18px 24px;
    }
    .explainer-body {
      padding: 0 24px 20px;
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
