import {
  area,
  Chart,
  ChartAxis,
  ChartGrid,
  line,
  useChart,
  xPixel,
} from "@astryxdesign/charts";
import "@astryxdesign/charts/charts.css";
import type { BurnPoint } from "../../../../factory-storyboard/schema/types";
import { axisLabel } from "./data";

/** Each line ends in a dot and its name, so the plot needs no legend. */
function EndLabels({ last }: { last: BurnPoint }) {
  const { xScale, yScale } = useChart();
  const x = xPixel({ date: axisLabel(last.date) }, "date", xScale);
  const shipped = yScale(last.shipped);
  // Keep the two labels apart when the lines meet.
  const scope = Math.min(yScale(last.total), shipped - 18);
  return (
    <g className="burn-up-ends">
      <circle cx={x} cy={yScale(last.total)} r="4" data-series="scope" />
      <circle cx={x} cy={shipped} r="4" data-series="shipped" />
      <text x={x + 12} y={scope} dominantBaseline="middle">
        <tspan>{last.total}</tspan> scope
      </text>
      <text x={x + 12} y={shipped} dominantBaseline="middle">
        <tspan>{last.shipped}</tspan> shipped
      </text>
    </g>
  );
}

export default function BurnUpPlot({ points }: { points: BurnPoint[] }) {
  return (
    <Chart
      aria-hidden="true"
      data={points.map((point) => ({ ...point, date: axisLabel(point.date) }))}
      xKey="date"
      series={[
        line("total", {
          label: "Scope",
          color: "var(--color-text-primary)",
          strokeWidth: 2,
          curve: "linear",
        }),
        // One series draws the wash and its line, so the tooltip lists it once.
        area("shipped", {
          label: "Shipped",
          color: "var(--factory-mint)",
          opacity: 1,
          curve: "linear",
        }),
      ]}
      height={300}
      margin={{ right: 104, left: 36 }}
      yBaseline="auto"
      grid={<ChartGrid horizontal />}
      axes={
        <>
          <ChartAxis position="left" tickCount={4} />
          <ChartAxis position="bottom" maxTicks={8} />
        </>
      }
      tooltip
    >
      <EndLabels last={points.at(-1)!} />
    </Chart>
  );
}
