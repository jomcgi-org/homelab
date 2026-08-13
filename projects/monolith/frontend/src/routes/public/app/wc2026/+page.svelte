<script>
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";

  let { data } = $props();

  // The whole tournament arrives in one payload; the visitor picks a country and
  // the page switches client-side with no refetch.
  const groups = $derived(data.groups ?? {});
  const allTeams = $derived(
    Object.values(groups)
      .flat()
      .sort((a, b) => a.name.localeCompare(b.name)),
  );
  const codeToGroup = $derived(
    Object.fromEntries(allTeams.map((t) => [t.fifa_code, t.group_name])),
  );
  const codeToName = $derived(
    Object.fromEntries(allTeams.map((t) => [t.fifa_code, t.name])),
  );

  // Selected country lives in the URL (?country=CODE) so a chosen team is
  // shareable and bookmarkable. It derives from $page.url (works in SSR and on
  // the client), falling back to the default (Scotland) when absent or unknown.
  // load() does not read url, so updating the query never re-runs it: switching
  // stays an instant client-side re-derive with no refetch.
  const requested = $derived($page.url.searchParams.get("country"));
  const selectedCode = $derived(
    requested && codeToGroup[requested]
      ? requested
      : (data.default_country ?? "SCO"),
  );
  const countryName = $derived(codeToName[selectedCode] ?? selectedCode);

  const groupName = $derived(codeToGroup[selectedCode] ?? "");
  const table = $derived(groups[groupName] ?? []);
  const q = $derived(data.qualification?.[selectedCode] ?? {});
  const swings = $derived(data.swing_by_country?.[selectedCode] ?? []);

  // Country dropdown: a neo-brutalist box in the headline that opens a
  // type-to-filter list. Closed by default (so the SSR screenshot is stable).
  let pickerOpen = $state(false);
  let pickerQuery = $state("");
  const pickerList = $derived(
    pickerQuery.trim()
      ? allTeams.filter((t) =>
          t.name.toLowerCase().includes(pickerQuery.trim().toLowerCase()),
        )
      : allTeams,
  );
  function choose(code) {
    // Mirror the pick into the URL (shareable). replaceState keeps every dropdown
    // flip out of browser history; keepFocus/noScroll avoid a jump. load() does
    // not depend on url, so this does not refetch.
    const url = new URL($page.url);
    url.searchParams.set("country", code);
    goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    pickerOpen = false;
    pickerQuery = "";
  }
  function togglePicker() {
    pickerOpen = !pickerOpen;
    pickerQuery = "";
  }

  // Each option's qualification status drives its dropdown affordance: a green
  // row for a team that has clinched a Round-of-32 place, a struck-through row
  // for one that is out. Undefined for teams still in contention (no styling).
  const statusOf = (code) => data.qualification?.[code]?.status;

  // Focus the search box the instant the panel opens, so a single click on the
  // country lets you start typing right away. The input is freshly mounted on
  // each open, so a use: action fires every time; an action keeps the a11y
  // linter quiet where the bare autofocus attribute would warn.
  function focusOnOpen(node) {
    node.focus();
  }

  // Probabilities arrive as raw 0..1 floats; the page is the only place they
  // become percentages.
  const pct = (x) => `${Math.round((x ?? 0) * 100)}%`;
  const points = (x) => Math.round((x ?? 0) * 100);

  // A contending team's headline must never round to a bare 100%/0%: those read
  // as certainty, but certainty is its own status (qualified / near_certain and
  // their mirrors). Clamp the live figure to 1..99% so the only routes to
  // "effectively certain" language are the dedicated tiers below.
  const pctClamped = (x) =>
    `${Math.min(99, Math.max(1, Math.round((x ?? 0) * 100)))}%`;

  // The headline reads the status first. Exact certainties show a verdict word;
  // the near_* tiers show a bounded ">99.9%"/"<0.1%" figure (no swing cards,
  // because no realistic combination of results changes the outcome); a live
  // contender shows its clamped qualify probability.
  const headline = $derived(
    q.status === "qualified"
      ? "Qualified"
      : q.status === "eliminated"
        ? "Eliminated"
        : q.status === "near_certain"
          ? ">99.9%"
          : q.status === "near_eliminated"
            ? "<0.1%"
            : pctClamped(q.prob_qualify),
  );

  // The swing section's empty-state copy depends on WHY there are no matches to
  // model: a settled or near-settled team has nothing left that can move it, and
  // a contending team can still have no cards when every remaining match leaves
  // its chances within a percentage point (all sub-threshold swings are dropped).
  const emptyMsg = $derived(
    q.status === "near_certain"
      ? `No realistic combination of remaining results can stop ${countryName} now.`
      : q.status === "near_eliminated"
        ? `No realistic combination of remaining results can save ${countryName} now.`
        : q.status === "qualified"
          ? `${countryName} has already qualified.`
          : q.status === "eliminated"
            ? `${countryName} can no longer qualify.`
            : q.status === "contention"
              ? `No remaining match meaningfully changes whether ${countryName} qualifies.`
              : "No remaining matches to model right now.",
  );

  const MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
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

  // Standings rows arrive pre-sorted, so finishing position is the row index.
  // World Cup groups are always four teams: the top two qualify automatically,
  // third place enters the best-third pool (eight of twelve advance), fourth is
  // out. The zone colours mirror the hero outcome bar (ink / accent / grey) so
  // the table reads as the same system, not a second unrelated widget.
  const zoneOf = (i) => (i < 2 ? "top2" : i === 2 ? "third" : "out");

  // Per-team live qualify chance, read from the same qualification map the
  // headline uses. Settled teams collapse to a verdict; the rest show a percent.
  const rowQual = (code) => data.qualification?.[code] ?? {};
  const gdClass = (gd) => (gd > 0 ? "gd-pos" : gd < 0 ? "gd-neg" : "gd-zero");

  // The current (unconditional) qualify chance. The swing cards plot each
  // outcome relative to this "Now" point.
  const baselinePct = $derived(pctNum(q.prob_qualify));

  // Hero outcome split. Top-two and best-third are the two qualifying routes;
  // the remainder is elimination. Integer percents that always sum to 100.
  const top2Pct = $derived(pctNum(q.prob_top2));
  const thirdPct = $derived(pctNum(q.prob_third));
  const elimPct = $derived(Math.max(0, 100 - top2Pct - thirdPct));

  // Thousands-formatted simulation count (pinned locale for a deterministic
  // SSR render / screenshot).
  const nSims = $derived(
    data.n_sims ? data.n_sims.toLocaleString("en-US") : "many",
  );

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
  <title>Will {countryName} get out of the World Cup group stage?</title>
  <meta
    name="description"
    content="Live odds that a 2026 World Cup team escapes the group stage, and the remaining matches that most change its chance. Elo-weighted Monte Carlo simulation."
  />
  <meta name="robots" content="noindex" />
</svelte:head>

<div class="page">
  <div class="board">
    <header class="board-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true"
              >&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">wc2026</span>
        </nav>
        {#if groupName}
          <p class="stats"><strong>Group {groupName}</strong></p>
        {/if}
      </div>
      <h1 class="title">
        Will <span class="picker" class:open={pickerOpen}>
          <button
            type="button"
            class="picker-btn"
            aria-haspopup="true"
            aria-expanded={pickerOpen}
            onclick={togglePicker}
          >
            {countryName}<span class="picker-caret" aria-hidden="true">▾</span>
          </button>
          {#if pickerOpen}
            <button
              type="button"
              class="picker-backdrop"
              aria-label="Close country list"
              onclick={() => (pickerOpen = false)}
            ></button>
            <span class="picker-panel">
              <input
                class="picker-search"
                type="text"
                placeholder="Type a country..."
                aria-label="Search countries"
                use:focusOnOpen
                bind:value={pickerQuery}
                onkeydown={(e) => {
                  if (e.key === "Escape") pickerOpen = false;
                  if (e.key === "Enter" && pickerList.length)
                    choose(pickerList[0].fifa_code);
                }}
                autocomplete="off"
              />
              <span class="picker-list">
                {#each pickerList as t (t.fifa_code)}
                  <button
                    type="button"
                    class="picker-opt"
                    class:sel={t.fifa_code === selectedCode}
                    class:qualified={statusOf(t.fifa_code) === "qualified"}
                    class:eliminated={statusOf(t.fifa_code) === "eliminated"}
                    title={statusOf(t.fifa_code) === "qualified"
                      ? "Qualified"
                      : statusOf(t.fifa_code) === "eliminated"
                        ? "Eliminated"
                        : null}
                    onclick={() => choose(t.fifa_code)}
                  >
                    {#if t.flag_url}
                      <img
                        class="flag"
                        src={t.flag_url}
                        alt=""
                        width="20"
                        height="14"
                        loading="lazy"
                      />
                    {/if}
                    <span class="picker-opt-name">{t.name}</span>
                    <span class="picker-opt-grp">{t.group_name}</span>
                  </button>
                {:else}
                  <span class="picker-empty">No team matches.</span>
                {/each}
              </span>
            </span>
          {/if}
        </span> get out of the World Cup group stage?
      </h1>
    </header>

    <!-- HEADLINE -->
    <section class="headline">
      <p class="headline-label">
        {countryName}'s chance of reaching the Round of 32
      </p>
      <p
        class="headline-figure"
        class:verdict={q.status === "qualified" || q.status === "eliminated"}
      >
        {headline}
      </p>

      <div
        class="outcome-bar"
        role="img"
        aria-label="Top-two finish {top2Pct}%, best third-place spot {thirdPct}%, eliminated {elimPct}%"
      >
        {#if top2Pct > 0}
          <div class="seg seg-top2" style="width:{top2Pct}%">
            <span class="seg-num">{top2Pct}%</span>
          </div>
        {/if}
        {#if thirdPct > 0}
          <div class="seg seg-third" style="width:{thirdPct}%">
            <span class="seg-num">{thirdPct}%</span>
            <span class="seg-label">Best third-place spot</span>
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
          <span class="key key-third"></span>Best third-place spot
          <strong>{thirdPct}%</strong>
        </li>
        <li>
          <span class="key key-elim"></span>Eliminated
          <strong>{elimPct}%</strong>
        </li>
      </ul>
    </section>

    <!-- HOW IT WORKS (expandable, directly under the headline) -->
    <details class="explainer">
      <summary class="explainer-summary">
        <span>How does this work?</span>
        <span class="explainer-toggle" aria-hidden="true"></span>
      </summary>
      <div class="explainer-body">
        <p>
          It's a Monte Carlo simulation. Every remaining group game is played
          out {nSims} times, and {countryName}'s chance is the share of those
          runs where they reach the Round of 32.
        </p>
        <ol class="explainer-steps">
          <li>
            <strong>Rate each team, with uncertainty.</strong> Every team starts from
            a pre-tournament Elo rating, then moves up or down with the group results
            already played, so a side that has over-performed carries that into its
            remaining games. Because a rating is an estimate and not a fact, each
            run draws the team's strength from a range around that value (wider for
            teams who have played fewer games), so a heavy favourite is never treated
            as a sure thing.
          </li>
          <li>
            <strong>Score each match.</strong> For an unplayed match, each team's
            Poisson scoring rate is built from both sides' drawn strengths and from
            how freely it has actually scored and conceded so far, so a strong attack
            and a leaky defence are modelled separately. Scorelines are sampled with
            a small low-score correction, so the stronger team scores more and evenly
            matched sides draw about a quarter of the time.
          </li>
          <li>
            <strong>Rank by the real rules.</strong> Each simulated tournament applies
            the actual 2026 rules: top two of every group, plus the eight best third-placed
            teams. Within a group, a tie on points is settled head-to-head first (the
            result between the level teams), then overall goal difference and goals
            scored; the third-placed teams, who never met, are compared on points,
            goal difference and goals scored.
          </li>
          <li>
            <strong>Aggregate over the runs.</strong> Across all {nSims} runs, the
            qualify chance, the split between the top-two and third-place routes,
            and each match's swing are all just counts from the same set of simulations.
          </li>
        </ol>
        <p class="explainer-fine">
          The two lowest FIFA tiebreakers, discipline and world ranking, are
          coin-flips here.
        </p>
      </div>
    </details>

    <!-- GROUP TABLE -->
    <section class="block">
      <h2 class="block-title">Group {groupName} table</h2>
      <div class="table-scroll">
        <table class="grid">
          <thead>
            <tr>
              <th class="col-pos" scope="col" title="Position">#</th>
              <th class="col-team" scope="col">Team</th>
              <th class="muted" scope="col">P</th>
              <th class="sec" scope="col">W</th>
              <th class="sec" scope="col">D</th>
              <th class="sec" scope="col">L</th>
              <th class="sec" scope="col">GF</th>
              <th class="sec" scope="col">GA</th>
              <th scope="col">GD</th>
              <th class="col-pts" scope="col">Pts</th>
              <th class="col-chance" scope="col">Chance</th>
            </tr>
          </thead>
          <tbody>
            {#each table as t, i (t.team_id)}
              {@const z = zoneOf(i)}
              {@const ql = rowQual(t.fifa_code)}
              <tr
                class:focus={t.fifa_code === selectedCode}
                class:cut={i === 1}
              >
                <td class="col-pos">
                  <span class="pos pos-{z}">{i + 1}</span>
                </td>
                <td class="col-team">
                  <span class="team">
                    {#if t.flag_url}
                      <img
                        class="flag"
                        src={t.flag_url}
                        alt=""
                        width="22"
                        height="15"
                        loading="lazy"
                      />
                    {/if}
                    <span class="team-name">{t.name}</span>
                  </span>
                </td>
                <td class="muted">{t.mp}</td>
                <td class="sec">{t.w}</td>
                <td class="sec">{t.d}</td>
                <td class="sec">{t.l}</td>
                <td class="sec">{t.gf}</td>
                <td class="sec">{t.ga}</td>
                <td class="gd {gdClass(t.gd)}">{fmtGd(t.gd)}</td>
                <td class="col-pts">{t.pts}</td>
                <td class="col-chance">
                  {#if ql.status === "qualified"}
                    <span class="verdict in">In</span>
                  {:else if ql.status === "eliminated"}
                    <span class="verdict out">Out</span>
                  {:else}
                    <span class="chance">
                      <span class="chance-num"
                        >{pctClamped(ql.prob_qualify)}</span
                      >
                      <span class="chance-bar">
                        <span
                          class="chance-fill"
                          style="width:{pctNum(ql.prob_qualify)}%"
                        ></span>
                      </span>
                    </span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <ul class="zone-legend">
        <li><span class="zone-key zone-top2"></span>1&ndash;2 qualify</li>
        <li>
          <span class="zone-key zone-third"></span>3rd: best third-place spot
        </li>
        <li><span class="zone-key zone-out"></span>4th: out</li>
      </ul>
    </section>

    <!-- SWING MATCHES -->
    <section class="block">
      <h2 class="block-title">Matches that could change it</h2>
      <p class="block-sub">
        Each remaining match, ranked by how much its result moves {countryName}'s
        qualify chance.
      </p>

      {#if swings.length === 0}
        <p class="empty">{emptyMsg}</p>
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
                  {codeToName[m.home_code] ?? m.home_code}
                  <span class="v">v</span>
                  {codeToName[m.away_code] ?? m.away_code}
                  {#if m.is_own_match}
                    <span class="badge own">{countryName}</span>
                  {/if}
                  <span class="badge grp">Group {m.group_name}</span>
                </span>
                <span
                  class="swing-mag"
                  title="How much this match moves {countryName}'s chance"
                >
                  <span class="swing-track">
                    <span class="swing-fill" style="width:{swingBar(m.swing)}%"
                    ></span>
                  </span>
                  &plusmn;{points(m.swing)} pts
                </span>
              </div>
              <p class="kickoff">{fmtKick(m.kickoff)}</p>

              <div class="line">
                <span class="line-track">
                  <span class="line-span" style="left:{lo}%; right:{100 - hi}%"
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
                  <span class="out-label"
                    >If {codeToName[m.home_code] ?? m.home_code} win</span
                  >
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
                  <span class="out-label"
                    >If {codeToName[m.away_code] ?? m.away_code} win</span
                  >
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

    <!-- FOOTER -->
    <footer class="foot">
      <p>
        Data from <a href="https://worldcup26.ir" rel="external noopener"
          >worldcup26.ir</a
        >. Odds from an Elo-weighted Monte Carlo, {nSims} simulations.
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
    line-height: 1.2;
    font-size: 30px;
  }

  /* ── Country picker (neo-brutalist inline dropdown) ─────── */
  .picker {
    position: relative;
    display: inline-block;
  }

  .picker-btn {
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
    margin: 0 2px;
    padding: 1px 8px 3px;
    font-family: var(--serif);
    font-size: inherit;
    line-height: 1;
    color: var(--ink);
    background: var(--accent);
    border: 2.5px solid var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
    cursor: pointer;
    transition:
      transform 0.05s ease,
      box-shadow 0.05s ease;
  }
  .picker-btn:hover {
    transform: translate(-1px, -1px);
    box-shadow: 4px 4px 0 var(--ink);
  }
  .picker.open .picker-btn {
    transform: translate(2px, 2px);
    box-shadow: 1px 1px 0 var(--ink);
  }
  .picker-caret {
    font-size: 0.55em;
    line-height: 1;
  }

  /* Full-viewport catcher so a click anywhere else closes the panel. */
  .picker-backdrop {
    position: fixed;
    inset: 0;
    z-index: 20;
    padding: 0;
    background: transparent;
    border: none;
    cursor: default;
  }

  .picker-panel {
    position: absolute;
    z-index: 21;
    top: calc(100% + 6px);
    left: 0;
    display: flex;
    flex-direction: column;
    width: 240px;
    max-width: 78vw;
    background: var(--paper);
    border: 2.5px solid var(--ink);
    box-shadow: 5px 5px 0 var(--ink);
  }

  .picker-search {
    margin: 0;
    padding: 8px 10px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
    background: var(--cream);
    border: none;
    border-bottom: 2px solid var(--ink);
    /* Strip the native input chrome: WebKit gives text inputs rounded corners
       and an inner bevel by default, which breaks the hard-edge brutalist box. */
    border-radius: 0;
    -webkit-appearance: none;
    appearance: none;
    outline: none;
  }
  .picker-search::placeholder {
    color: var(--ink-3);
  }

  .picker-list {
    display: flex;
    flex-direction: column;
    max-height: 260px;
    overflow-y: auto;
  }

  .picker-opt {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
    font-family: var(--mono);
    font-size: 13px;
    text-align: left;
    color: var(--ink);
    background: var(--paper);
    border: none;
    border-bottom: 1px solid var(--rule);
    cursor: pointer;
  }
  /* Qualified: a calm green wash (overridden by hover/selection below, which
     share specificity, so source order lets those win when active). Eliminated:
     the name is struck through in coral and muted, an affordance that survives
     hover and selection since it styles the text, not the row background. */
  .picker-opt.qualified {
    background: color-mix(in srgb, var(--green) 32%, var(--paper));
  }
  .picker-opt.qualified .picker-opt-name {
    font-weight: 700;
  }
  .picker-opt.eliminated .picker-opt-name {
    text-decoration: line-through;
    text-decoration-thickness: 2px;
    text-decoration-color: var(--coral);
    color: var(--ink-3);
  }
  .picker-opt:hover {
    background: var(--blue);
  }
  .picker-opt.sel {
    background: var(--accent);
    font-weight: 700;
  }
  .picker-opt-name {
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .picker-opt-grp {
    font-size: 11px;
    color: var(--ink-3);
  }
  .picker-opt.sel .picker-opt-grp {
    color: var(--ink);
  }
  .picker-empty {
    padding: 10px;
    font-family: var(--mono);
    font-size: 12px;
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
    /* Tabular figures so every column of digits lines up vertically. */
    font-variant-numeric: tabular-nums;
  }

  .grid th {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--ink-3);
    text-align: right;
    padding: 7px 6px;
    border-bottom: 2px solid var(--ink);
    white-space: nowrap;
  }

  .grid td {
    text-align: right;
    padding: 9px 6px;
    border-bottom: 1px solid var(--rule);
    white-space: nowrap;
  }

  .grid tbody tr:last-child td {
    border-bottom: none;
  }

  /* The automatic-qualification line: a heavy rule under 2nd place separates the
     two teams who are through from the rest. */
  .grid tbody tr.cut td {
    border-bottom: 3px solid var(--ink);
  }

  /* Secondary stats (W/D/L/GF/GA) and matches-played are de-emphasised so the
     eye lands on points, goal difference and the live chance. */
  .grid .muted,
  .grid .sec {
    color: var(--ink-3);
  }

  .col-team {
    text-align: left !important;
    width: 100%;
  }

  /* ── Position badge ──────────────────────── */
  .col-pos {
    text-align: center !important;
    padding-left: 10px;
    padding-right: 4px;
  }

  .pos {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 21px;
    height: 21px;
    font-size: 11px;
    font-weight: 700;
    border: 1.5px solid var(--ink);
  }

  /* Zone colours mirror the hero outcome bar above. */
  .pos-top2 {
    background: var(--ink);
    color: var(--paper);
  }

  .pos-third {
    background: var(--accent);
    color: var(--ink);
  }

  .pos-out {
    background: var(--paper);
    color: var(--ink-3);
    border-color: var(--rule);
  }

  .col-pts {
    font-weight: 700;
    font-size: 15px;
  }

  .grid th.col-pts {
    color: var(--ink);
  }

  /* Goal difference: negatives flagged coral, the rest stays neutral (the "+"
     already signals a positive). */
  .gd-neg {
    color: var(--coral);
  }
  .gd-zero {
    color: var(--ink-3);
  }

  .team {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    max-width: 100%;
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

  /* ── Live qualify chance ─────────────────── */
  .col-chance {
    min-width: 56px;
  }

  .chance {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 4px;
    width: 100%;
  }

  .chance-num {
    font-weight: 700;
    line-height: 1;
  }

  .chance-bar {
    display: block;
    width: 100%;
    max-width: 120px;
    height: 5px;
    background: var(--rule);
    border: 1px solid var(--ink);
  }

  .chance-fill {
    display: block;
    height: 100%;
    background: var(--ink);
  }

  .verdict {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 2px 8px;
    border: 1.5px solid var(--ink);
  }

  .verdict.in {
    background: color-mix(in srgb, var(--green) 55%, var(--paper));
  }

  .verdict.out {
    background: var(--rule);
    color: var(--ink-3);
  }

  /* Selected team: a light accent wash plus a solid ink bar down the left edge.
     A wash (not the full accent fill) keeps this distinct from the solid-accent
     3rd-place zone badge, which matters because the default team (Scotland) is
     itself 3rd, so a full-accent row would swallow its own zone marker. */
  .grid tbody tr.focus td {
    background: color-mix(in srgb, var(--accent) 32%, var(--paper));
    border-bottom-color: var(--ink);
  }

  .grid tbody tr.focus td:first-child {
    box-shadow: inset 3px 0 0 var(--ink);
  }

  .grid tbody tr.focus.cut td {
    border-bottom-width: 3px;
  }

  /* ── Qualification-zone legend ───────────── */
  .zone-legend {
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  .zone-legend li {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .zone-key {
    width: 11px;
    height: 11px;
    border: 1.5px solid var(--ink);
    flex-shrink: 0;
  }

  .zone-top2 {
    background: var(--ink);
  }
  .zone-third {
    background: var(--accent);
  }
  .zone-out {
    background: var(--paper);
    border-color: var(--rule);
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

  /* On phones the per-result breakdown (W/D/L/GF/GA) would force a horizontal
     scroll, which is worse than dropping it. Keep position, team, played, GD,
     points and the live chance, the columns that actually answer "who's going
     through". The full breakdown returns on wider screens. */
  @media (max-width: 519px) {
    .grid .sec {
      display: none;
    }
  }

  /* From tablet up the board is wide enough that an auto-width team column eats
     all the slack and strands the stats against the right edge, leaving a big
     gap in the middle. Switch to a fixed layout: pin the structural columns and
     let P/W/D/L/GF/GA/GD share the remainder, so the numbers spread evenly
     across the width like a proper standings table. */
  @media (min-width: 520px) {
    .grid {
      table-layout: fixed;
    }
    .col-team {
      width: 27%;
    }
    .col-pos {
      width: 6%;
    }
    .col-pts {
      width: 8%;
    }
    .col-chance {
      width: 13%;
    }
    .team-name {
      overflow: hidden;
      text-overflow: ellipsis;
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
