const SVG_WIDTH = 600;
const SVG_HEIGHT = 120;
const LEFT = 34;
const BOTTOM = 20;
const TOP = 6;

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

export function chartScale(maximum) {
  const max = finite(maximum);
  if (max === 0) return { step: 1, top: 1 };
  const magnitude = 10 ** Math.floor(Math.log10(max));
  const step =
    [1, 2, 5]
      .map((multiple) => multiple * magnitude)
      .find((candidate) => max / candidate <= 5) ?? magnitude * 10;
  return { step, top: Math.ceil(max / step) * step };
}

export function sparkPaths(values, width = 100, height = 24) {
  const safe = values.length ? values.map(finite) : [0];
  const max = Math.max(1, ...safe);
  const denominator = Math.max(1, safe.length - 1);
  const points = safe.map((value, index) => [
    (index / denominator) * width,
    height - 2 - (value / max) * (height - 4),
  ]);
  const line = points
    .map(
      ([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`,
    )
    .join("");
  const [endX, endY] = points.at(-1);
  return {
    line,
    area: `${line}L${width} ${height}L0 ${height}Z`,
    endX: endX.toFixed(1),
    endY: endY.toFixed(1),
  };
}

export function sparkSvg(values) {
  const path = sparkPaths(values);
  return `<svg class="sp" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><path class="area" d="${path.area}"/><path d="${path.line}"/><circle class="end" cx="${path.endX}" cy="${path.endY}" r="2"/></svg>`;
}

function isoDay(value) {
  const day = String(value ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const parsed = new Date(`${day}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === day
    ? day
    : null;
}

function safeColor(value) {
  return /^var\(--[a-z0-9-]+\)$/.test(value) ? value : "currentColor";
}

function safeId(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "");
}

function completeDays(rows) {
  const valid = rows.filter((row) => isoDay(row.d));
  if (!valid.length) return [];
  const first = new Date(`${valid[0].d}T00:00:00Z`);
  const last = new Date(`${valid.at(-1).d}T00:00:00Z`);
  const days = [];
  for (const day = first; day <= last; day.setUTCDate(day.getUTCDate() + 1)) {
    days.push(day.toISOString().slice(0, 10));
  }
  return days;
}

export function barChartSvg(id, rows, series, colors) {
  const sorted = rows
    .map((row) => ({ ...row, d: isoDay(row.d) }))
    .filter((row) => row.d)
    .sort((a, b) => a.d.localeCompare(b.d));
  const days = completeDays(sorted);
  const byDay = Object.fromEntries(sorted.map((row) => [row.d, row]));
  const total = (row) =>
    series.reduce((sum, key) => sum + finite(row?.[key]), 0);
  const maximum = Math.max(0, ...days.map((day) => total(byDay[day])));
  const { step, top } = chartScale(maximum);
  const y = (value) =>
    TOP + (SVG_HEIGHT - TOP - BOTTOM) * (1 - finite(value) / top);
  const barWidth = days.length ? (SVG_WIDTH - LEFT - 4) / days.length : 0;
  const patternId = `hatch-${safeId(id)}`;
  let svg = `<svg viewBox="0 0 ${SVG_WIDTH} ${SVG_HEIGHT}" role="img" aria-label="Bar chart">`;
  svg += `<defs><pattern id="${patternId}" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="4" stroke="currentColor" stroke-width="1" opacity="0.55"/></pattern></defs>`;
  for (let value = 0; value <= top; value += step) {
    svg += `<line x1="${LEFT}" y1="${y(value)}" x2="${SVG_WIDTH}" y2="${y(value)}" stroke="currentColor" stroke-width="0.5" opacity="${value === 0 ? 0.8 : 0.18}"/><text x="${LEFT - 6}" y="${y(value) + 3.5}" font-size="9" text-anchor="end" fill="currentColor" opacity="0.7">${value}</text>`;
  }
  days.forEach((day, dayIndex) => {
    const row = byDay[day];
    let accumulated = 0;
    const x = LEFT + dayIndex * barWidth + 1;
    series.forEach((key, seriesIndex) => {
      const value = finite(row?.[key]);
      if (!value) return;
      const color = colors[seriesIndex];
      const fill = color === "hatch" ? `url(#${patternId})` : safeColor(color);
      svg += `<rect x="${x}" y="${y(accumulated + value)}" width="${Math.max(0, barWidth - 2)}" height="${y(accumulated) - y(accumulated + value)}" fill="${fill}" stroke="${color === "hatch" ? "currentColor" : "none"}" stroke-width="0.5"/>`;
      accumulated += value;
    });
    if (dayIndex % 7 === 0 || dayIndex === days.length - 1) {
      const label = day.slice(5).replace("-", "·");
      svg += `<text x="${x + (barWidth - 2) / 2}" y="${SVG_HEIGHT - 6}" font-size="9" text-anchor="middle" fill="currentColor" opacity="0.7">${label}</text>`;
    }
  });
  return `${svg}</svg>`;
}

function todayUtc(now) {
  if (now instanceof Date && Number.isFinite(now.getTime())) {
    return now.toISOString().slice(0, 10);
  }
  return isoDay(now) ?? new Date().toISOString().slice(0, 10);
}

export function window(rows, now, count = 30) {
  const byDay = new Map();
  for (const row of rows) {
    const day = isoDay(row.d ?? row.day);
    if (day) byDay.set(day, row);
  }
  const end = new Date(`${todayUtc(now)}T00:00:00Z`);
  const days = [];
  for (let offset = count - 1; offset >= 0; offset -= 1) {
    const day = new Date(end);
    day.setUTCDate(day.getUTCDate() - offset);
    const d = day.toISOString().slice(0, 10);
    days.push({ d, ...(byDay.get(d) ?? {}) });
  }
  return days;
}

export function last14(rows, key, now) {
  return window(rows, now, 14).map((row) => finite(row[key]));
}
