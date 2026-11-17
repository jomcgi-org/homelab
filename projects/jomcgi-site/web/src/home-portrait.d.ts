export function createPortrait(options: {
  canvas: HTMLCanvasElement;
  host: HTMLElement;
  tapTarget: HTMLElement;
  src: string;
  scheme: () => "light" | "dark";
  reduceMotion?: boolean;
}): { destroy(): void };
