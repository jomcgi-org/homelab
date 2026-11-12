import {
  createContext,
  useCallback,
  useContext,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Selector } from "@astryxdesign/core/Selector";
import { Theme } from "@astryxdesign/core/theme";
import { sheetTheme } from "../theme";

type Appearance = "system" | "light" | "dark";
const storageKey = "grimoire.appearance";
const AppearanceContext = createContext<{
  preference: Appearance;
  select: (value: string) => void;
} | null>(null);

function isAppearance(value: unknown): value is Appearance {
  return value === "system" || value === "light" || value === "dark";
}

function readPreference(): Appearance {
  try {
    const saved = window.localStorage.getItem(storageKey);
    return isAppearance(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

/** Owns the device-local preference for every entry point, without remounting routes. */
export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [preference, setPreference] = useState(readPreference);
  const [media] = useState(() =>
    typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null,
  );
  const subscribe = useCallback(
    (changed: () => void) => {
      if (preference !== "system" || !media) return () => {};
      media.addEventListener("change", changed);
      return () => media.removeEventListener("change", changed);
    },
    [media, preference],
  );
  const snapshot = useCallback(() => media?.matches ?? false, [media]);
  const systemDark = useSyncExternalStore(subscribe, snapshot);
  const mode =
    preference === "system" ? (systemDark ? "dark" : "light") : preference;

  const select = (value: string) => {
    if (!isAppearance(value)) return;
    setPreference(value);
    try {
      window.localStorage.setItem(storageKey, value);
    } catch {
      // Storage may be disabled or full. The choice still works for this mount.
    }
  };

  // Astryx synchronizes the root attributes in a layout effect, before paint.
  // Its root scope includes the body, portaled overlays and the toast viewport.
  return (
    <AppearanceContext.Provider value={{ preference, select }}>
      <Theme theme={sheetTheme} mode={mode}>
        {children}
      </Theme>
    </AppearanceContext.Provider>
  );
}

export function AppearanceControl() {
  const appearance = useContext(AppearanceContext);
  if (!appearance)
    throw new Error("AppearanceControl needs AppearanceProvider");
  return (
    <Selector
      label="Appearance"
      variant="secondary"
      size="sm"
      value={appearance.preference}
      onChange={appearance.select}
      options={[
        { value: "system", label: "System" },
        { value: "light", label: "Light" },
        { value: "dark", label: "Dark" },
      ]}
    />
  );
}
