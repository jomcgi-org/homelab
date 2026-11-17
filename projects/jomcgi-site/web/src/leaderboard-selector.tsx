import { useState } from "react";
import {
  PRESETS,
  PROVIDERS,
  matchPreset,
  presetIds,
  provider,
  providerSlot,
  rank,
  shortName,
} from "./leaderboard-model";
import type { Model } from "./leaderboard-model";

export function Selector({
  models,
  selected,
  onChange,
}: {
  models: Model[];
  selected: Set<string>;
  onChange: (ids: Set<string>) => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const active = matchPreset(models, selected);
  const q = query.trim().toLowerCase();
  const matches = models.filter((m) =>
    `${shortName(m)} ${m.id}`.toLowerCase().includes(q),
  );
  const byProvider = new Map<string, Model[]>();
  for (const m of rank(matches)) {
    const key = provider(m.id);
    byProvider.set(key, [...(byProvider.get(key) ?? []), m]);
  }
  const order = (p: string) => {
    const i = PROVIDERS.indexOf(p);
    return i === -1 ? PROVIDERS.length : i;
  };
  const groups = [...byProvider.entries()].sort(
    (a, b) => order(a[0]) - order(b[0]),
  );
  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  }
  const selectMatches = () =>
    onChange(new Set([...selected, ...matches.map((m) => m.id)]));
  return (
    <section className="selector">
      <h2 className="sec-label">
        / Models{" "}
        <span className="aside num">
          {selected.size} of {models.length} shown
        </span>
      </h2>
      <div className="row">
        <div className="seg" role="group" aria-label="Model presets">
          {PRESETS.map((preset) => (
            <button
              key={preset.key}
              type="button"
              aria-pressed={active === preset.key}
              onClick={() => onChange(presetIds(models, preset.key))}
            >
              {preset.label}
            </button>
          ))}
        </div>
        <input
          className="search"
          type="search"
          placeholder="Search models"
          aria-label="Search models"
          value={query}
          onChange={(event) => {
            setQuery(event.currentTarget.value);
            if (event.currentTarget.value.trim()) setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") setQuery("");
            if (event.key === "Enter" && q) selectMatches();
          }}
        />
        {q && (
          <button type="button" className="act" onClick={selectMatches}>
            Add {matches.length} matching
          </button>
        )}
        <button
          type="button"
          className="act"
          disabled={!selected.size}
          onClick={() => onChange(new Set())}
        >
          Clear all
        </button>
        {!active && selected.size > 0 && <span className="custom">custom</span>}
      </div>
      <details
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>Choose models</summary>
        {!matches.length && (
          <p className="none">No model matches “{query.trim()}”.</p>
        )}
        <div className="groups">
          {groups.map(([name, list]) => (
            <fieldset key={name}>
              <legend>
                <i className="sw" data-slot={providerSlot(list[0]!.id)} />
                {name}
              </legend>
              {list.map((m) => (
                <label key={m.id}>
                  <input
                    type="checkbox"
                    checked={selected.has(m.id)}
                    onChange={() => toggle(m.id)}
                  />
                  <span>{shortName(m)}</span>
                  {m.role === "anchor" ? (
                    <em>ceiling</em>
                  ) : m.self_hosted ? (
                    <em>local</em>
                  ) : null}
                </label>
              ))}
            </fieldset>
          ))}
        </div>
      </details>
    </section>
  );
}
