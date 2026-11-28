import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { TextInput } from "@astryxdesign/core/TextInput";
import { ToggleButton } from "@astryxdesign/core/ToggleButton";
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
            <ToggleButton
              key={preset.key}
              label={preset.label}
              isPressed={active === preset.key}
              onPressedChange={() => onChange(presetIds(models, preset.key))}
            />
          ))}
        </div>
        <TextInput
          isLabelHidden
          type="search"
          placeholder="Search models"
          label="Search models"
          value={query}
          onChange={(value) => {
            setQuery(value);
            if (value.trim()) setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") setQuery("");
            if (event.key === "Enter" && q) selectMatches();
          }}
        />
        {q && (
          <Button
            label={`Add ${matches.length} matching`}
            onClick={selectMatches}
          />
        )}
        <Button
          label="Clear all"
          isDisabled={!selected.size}
          onClick={() => onChange(new Set())}
        />
        {!active && selected.size > 0 && <span className="custom">custom</span>}
      </div>
      <Collapsible trigger="Choose models" isOpen={open} onOpenChange={setOpen}>
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
                <ToggleButton
                  key={m.id}
                  label={[
                    shortName(m),
                    m.role === "anchor"
                      ? "ceiling"
                      : m.self_hosted
                        ? "local"
                        : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  isPressed={selected.has(m.id)}
                  onPressedChange={() => toggle(m.id)}
                />
              ))}
            </fieldset>
          ))}
        </div>
      </Collapsible>
    </section>
  );
}
