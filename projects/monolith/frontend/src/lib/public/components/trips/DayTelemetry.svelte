<script>
  // Per-photo telemetry panel: the monospace "machine interface" readout that
  // sat beside the photo in the old React day view (DataPanel). Each cell is a
  // brutalist 2px-bordered box with a mono eyebrow label and a value. `t` is the
  // object from lib/trips/telemetry.js photoTelemetry(); `index`/`total` drive
  // the photo counter, `dayColor` tints the accent border.
  import { formatCoord } from "$lib/trips/telemetry.js";

  let { t = null, index = 0, total = 0, dayColor = "var(--ink)" } = $props();
</script>

{#if t}
  <div class="telem" style={`--day:${dayColor}`}>
    <div class="cell time">
      <span class="label">Time</span>
      <span class="clock">{t.time}<span class="period">{t.period}</span></span>
    </div>

    <div class="cell">
      <span class="label">Solar</span>
      <span class="value">{t.solarAltDeg != null ? `${Math.round(t.solarAltDeg)}°` : "--"}</span>
      <span class="sub">{t.solarLabel}</span>
    </div>

    <div class="cell">
      <span class="label">Light</span>
      <span class="value sm">{t.light || "DARK"}</span>
    </div>

    <div class="cell">
      <span class="label">EV</span>
      <span class="value">{t.ev ?? "--"}</span>
      <span class="sub">{t.evLabel}</span>
    </div>

    <div class="cell">
      <span class="label">Elev</span>
      <span class="value">{t.elevation != null ? Math.round(t.elevation) : "--"}<span class="unit">m</span></span>
    </div>

    <div class="cell">
      <span class="label">Position</span>
      <span class="coord">{formatCoord(t.lat, true)}</span>
      <span class="coord">{formatCoord(t.lng, false)}</span>
    </div>

    <div class="cell">
      <span class="label">Km</span>
      <span class="value">{t.km ?? 0}<span class="unit">/{t.totalKm ?? 0}</span></span>
    </div>

    <div class="cell bearing">
      <span class="label">Bearing</span>
      <span class="arrow">{t.bearingArrow}</span>
      <span class="sub">{t.bearing != null ? `${Math.round(t.bearing)}°` : "--"}</span>
    </div>

    <div class="cell optics">
      <span class="label">Optics</span>
      <span class="value sm">
        {t.focalLength35mm != null ? `${t.focalLength35mm}mm` : "--"} &fnof;/{t.aperture ?? "--"}
      </span>
      <span class="sub">ISO {t.iso ?? "--"} · {t.shutterSpeed ?? "--"}</span>
    </div>

    <div class="cell">
      <span class="label">Photo</span>
      <span class="value">{total ? index + 1 : 0}<span class="unit">/{total}</span></span>
    </div>
  </div>
{/if}

<style>
  .telem {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    border: 2px solid var(--ink);
    border-bottom: none;
    font-family: var(--mono);
  }
  .cell {
    display: flex;
    flex-direction: column;
    padding: 12px 14px;
    border-bottom: 2px solid var(--ink);
    border-right: 2px solid var(--ink);
    background: var(--paper);
    min-width: 0;
  }
  .label {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 6px;
  }
  .value {
    font-size: 20px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .value.sm {
    font-size: 13px;
    font-weight: 700;
  }
  .unit {
    font-size: 11px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 2px;
  }
  .sub {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--ink-3);
    margin-top: 4px;
  }
  .coord {
    font-size: 11px;
    font-weight: 700;
    line-height: 1.5;
    color: var(--ink);
  }
  /* TIME: the dominant readout, spanning a touch wider with a big clock. */
  .time {
    border-top: 4px solid var(--day);
    margin-top: -2px;
  }
  .clock {
    font-size: 28px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .period {
    font-size: 12px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 4px;
  }
  .bearing .arrow {
    font-size: 30px;
    line-height: 1;
    color: var(--ink);
  }
  .optics {
    /* Optics text is long: let it claim two columns on wider grids. */
    grid-column: span 2;
  }
  @media (max-width: 600px) {
    .optics {
      grid-column: span 1;
    }
  }
</style>
