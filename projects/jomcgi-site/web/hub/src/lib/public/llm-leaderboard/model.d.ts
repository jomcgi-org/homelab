// Shared view-model contract for the Svelte hub and the static React page.
export interface Model {
  id: string;
  name?: string;
  role?: string;
  self_hosted?: boolean;
  qualified?: boolean;
  hard_pass: number;
  hard_n: number;
  floor_pass: number;
  floor_n: number;
  cost_usd?: number | null;
  cost_per_solve_usd?: number | null;
  cost_estimated?: boolean;
  mean_latency_ms?: number | null;
  mean_tokens?: number | null;
  mean_turns?: number | null;
  tool_use_ok?: number | null;
  errored_tasks?: string[];
  tasks?: { id: string; passed: boolean }[];
}
export interface Task {
  id: string;
  tier: string;
  real_test: boolean;
  blurb: string;
  passed: number;
  n: number;
}
export interface AgentIndex {
  roles: Record<string, unknown>;
  models: Record<
    string,
    {
      low_n?: string[];
      roles: Record<
        string,
        {
          index: number | null;
          ci?: [number, number];
          coverage?: number;
        }
      >;
    }
  >;
  picks?: Record<
    string,
    { best?: string; cheapest?: string; fastest?: string }
  >;
  min_n: number;
}
export interface IndexedModel extends Model {
  index_value: number;
  index_ci?: [number, number];
  index_coverage?: number;
  index_low_n: string[];
}
export interface Leaderboard {
  generated_at: string;
  harness_version: string;
  models: Model[];
  tasks: Task[];
  index?: AgentIndex;
}
export type MetricKey = "hard" | "cost" | "wall" | "tokens" | "turns";
export interface Metric {
  label: string;
  unit: string;
  better: "higher" | "lower";
  get: (model: Model) => number | null | undefined;
  fmt: (value: number | null | undefined) => string;
  tick?: (value: number) => string;
  note: (model: Model) => string;
  log?: boolean;
  candidatesOnly?: boolean;
}
export const PROVIDERS: string[];
export const PRESETS: { key: string; label: string }[];
export const METRICS: Record<MetricKey, Metric>;
export function provider(id: string): string;
export function providerSlot(id: string): string;
export function shortName(model: Pick<Model, "id" | "name">): string;
export function hardRate(model: Model): number;
export function rank<T extends Model>(models: T[]): T[];
export function presetIds(models: Model[], key: string): Set<string>;
export function matchPreset(models: Model[], ids: Set<string>): string | null;
export function parseSelection(
  models: Model[],
  raw: string | null,
): Set<string>;
export function serializeSelection(
  models: Model[],
  ids: Set<string>,
): string | null;
export function money(value: number | null | undefined): string;
export function secs(value: number | null | undefined): string;
export function kfmt(value: number | null | undefined): string;
export function fmtDate(iso: string): string;
export function plottable<T extends Model>(models: T[], key: MetricKey): T[];
export function sortByMetric(models: Model[], key: MetricKey): Model[];
export function paretoFrontier<T extends { x: number; y: number }>(
  points: T[],
): T[];
export function providersIn(models: { id: string }[]): string[];
export function cellState(
  model: Model,
  taskId: string,
): "pass" | "fail" | "errored" | "none";
export function orderedTasks(tasks: Task[]): (Task & { no: number })[];
export function indexRoles(index: AgentIndex | null | undefined): string[];
export function roleIndex(
  index: AgentIndex | null | undefined,
  role: string,
  modelId: string,
): AgentIndex["models"][string]["roles"][string] | null;
export function indexRows(
  models: Model[],
  index: AgentIndex | null | undefined,
  role: string,
): IndexedModel[];
