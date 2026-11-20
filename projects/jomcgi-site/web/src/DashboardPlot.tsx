import { bar, Chart, ChartAxis, ChartGrid } from "@astryxdesign/charts";
import "@astryxdesign/charts/charts.css";
import type { ChartPoint } from "./DashboardChart";

export default function DashboardPlot({
  points,
  unit,
  maximum,
}: {
  points: readonly ChartPoint[];
  unit: string;
  maximum?: number;
}) {
  // Consecutive measurements can share a label. Keep each in its own slot.
  const data = points.map(({ value }, index) => ({
    slot: String(index),
    value,
  }));
  const labelFor = (slot: unknown) => points[Number(slot)]?.label ?? "";

  return (
    <Chart
      aria-hidden="true"
      data={data}
      xKey="slot"
      series={[bar("value", { label: unit })]}
      height={200}
      yBaseline="zero"
      yDomain={maximum === undefined ? undefined : [0, maximum]}
      grid={<ChartGrid horizontal />}
      axes={
        <>
          <ChartAxis position="left" tickCount={4} />
          <ChartAxis position="bottom" tickFormat={labelFor} />
        </>
      }
    />
  );
}
