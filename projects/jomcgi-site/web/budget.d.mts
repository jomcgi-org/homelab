export interface PageBudget {
  page: string;
  bytes: number;
}

export declare const BUDGET_BYTES: number;
export declare function initialScripts(html: string): string[];
export declare function measure(dist: string): PageBudget[];
export declare function overBudget(results: PageBudget[]): PageBudget[];
