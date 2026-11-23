import {
  bar,
  Chart,
  ChartAxis,
  isBandScale,
  useChart,
} from "@astryxdesign/charts";
import "@astryxdesign/charts/charts.css";
import type { ChartPoint } from "./DashboardChart";

const maximumBarWidth = 48;
/** Columns this narrow have no room for a value on the cap. */
const minimumLabelledStep = 28;

/** Each column carries its value, so the plot needs no value axis or grid. */
function CapValues({ format }: { format: (value: number) => string }) {
  const { data, xScale, yScale } = useChart();
  if (!isBandScale(xScale) || xScale.step() < minimumLabelledStep) return null;
  return (
    <g className="dashboard-chart-values">
      {data.map((row) => {
        const slot = String(row.slot);
        const value = Number(row.value);
        return (
          <text
            key={slot}
            x={(xScale(slot) ?? 0) + xScale.bandwidth() / 2}
            y={yScale(value) - 6}
            textAnchor="middle"
          >
            {format(value)}
          </text>
        );
      })}
    </g>
  );
}

export default function DashboardPlot({
  points,
  unit,
  maximum,
  formatValue,
  formatLabel = (label) => label,
}: {
  points: readonly ChartPoint[];
  unit: string;
  maximum?: number;
  formatValue: (value: number) => string;
  formatLabel?: (label: string) => string;
}) {
  // Consecutive measurements can share a label. Keep each in its own slot.
  const data = points.map(({ value }, index) => ({
    slot: String(index),
    value,
  }));
  const labelFor = (slot: unknown) =>
    formatLabel(points[Number(slot)]?.label ?? "");
  const valueBars = bar("value", {
    label: unit,
    radius: 0,
    color: "var(--color-accent)",
  });
  const resolve = valueBars.resolve;
  valueBars.resolve = (context, stack, group) => {
    const { xScale } = context;
    if (!isBandScale(xScale) || xScale.bandwidth() <= maximumBarWidth) {
      return resolve(context, stack, group);
    }
    // Increase both paddings to keep the original step and band centers.
    const extraPadding = (xScale.bandwidth() - maximumBarWidth) / xScale.step();
    const cappedScale = xScale
      .copy()
      .paddingInner(xScale.paddingInner() + extraPadding)
      .paddingOuter(xScale.paddingOuter() + extraPadding / 2);
    return resolve({ ...context, xScale: cappedScale }, stack, group);
  };
  const top = maximum ?? Math.max(1, ...points.map(({ value }) => value));

  return (
    <Chart
      aria-hidden="true"
      data={data}
      xKey="slot"
      series={[valueBars]}
      height={200}
      margin={{ top: 24, right: 0, left: 0 }}
      yDomain={[0, top]}
      axes={<ChartAxis position="bottom" tickFormat={labelFor} />}
    >
      <CapValues format={formatValue} />
    </Chart>
  );
}
