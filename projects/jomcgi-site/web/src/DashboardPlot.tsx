import {
  bar,
  Chart,
  ChartAxis,
  ChartGrid,
  isBandScale,
} from "@astryxdesign/charts";
import "@astryxdesign/charts/charts.css";
import type { ChartPoint } from "./DashboardChart";

export default function DashboardPlot({
  points,
  unit,
  maximum,
  maximumBarWidth,
}: {
  points: readonly ChartPoint[];
  unit: string;
  maximum?: number;
  maximumBarWidth?: number;
}) {
  // Consecutive measurements can share a label. Keep each in its own slot.
  const data = points.map(({ value }, index) => ({
    slot: String(index),
    value,
  }));
  const labelFor = (slot: unknown) => points[Number(slot)]?.label ?? "";
  const valueBars = bar("value", { label: unit });
  if (maximumBarWidth !== undefined) {
    const resolve = valueBars.resolve;
    valueBars.resolve = (context, stack, group) => {
      const { xScale } = context;
      if (!isBandScale(xScale) || xScale.bandwidth() <= maximumBarWidth) {
        return resolve(context, stack, group);
      }
      // Increase both paddings to keep the original step and band centers.
      const extraPadding =
        (xScale.bandwidth() - maximumBarWidth) / xScale.step();
      const cappedScale = xScale
        .copy()
        .paddingInner(xScale.paddingInner() + extraPadding)
        .paddingOuter(xScale.paddingOuter() + extraPadding / 2);
      return resolve({ ...context, xScale: cappedScale }, stack, group);
    };
  }

  return (
    <Chart
      aria-hidden="true"
      data={data}
      xKey="slot"
      series={[valueBars]}
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
