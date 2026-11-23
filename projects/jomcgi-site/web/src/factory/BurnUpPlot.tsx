import { area, Chart, ChartAxis, ChartGrid, line } from "@astryxdesign/charts";
import "@astryxdesign/charts/charts.css";
import type { BurnPoint } from "../../../../factory-storyboard/schema/types";
import { axisLabel } from "./data";

export default function BurnUpPlot({ points }: { points: BurnPoint[] }) {
  return (
    <Chart
      aria-hidden="true"
      data={points.map((point) => ({ ...point, date: axisLabel(point.date) }))}
      xKey="date"
      series={[
        line("total", {
          label: "Scope",
          color: "var(--color-text-secondary)",
          strokeWidth: 2,
          curve: "linear",
        }),
        // One series draws the wash and its line, so the tooltip lists it once.
        area("shipped", {
          label: "Shipped",
          color: "var(--color-accent)",
          opacity: 0.12,
          curve: "linear",
        }),
      ]}
      height={280}
      yBaseline="auto"
      grid={<ChartGrid horizontal />}
      axes={
        <>
          <ChartAxis position="left" tickCount={4} />
          <ChartAxis position="bottom" maxTicks={8} />
        </>
      }
      tooltip
    />
  );
}
