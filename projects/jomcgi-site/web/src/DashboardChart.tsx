import { lazy, Suspense, useId } from "react";
import "./dashboard-chart.css";

export type ChartPoint = { label: string; value: number };

const DashboardPlot = lazy(() => import("./DashboardPlot"));

export function DashboardChart({
  title,
  unit,
  points,
  maximum,
  maximumBarWidth,
  formatValue,
}: {
  title: string;
  unit: string;
  points: readonly ChartPoint[];
  maximum?: number;
  maximumBarWidth?: number;
  formatValue: (value: number) => string;
}) {
  const id = useId();

  return (
    <figure className="chart" aria-labelledby={id}>
      <figcaption id={id}>{title}</figcaption>
      {points.length === 0 ? (
        <p>No measurements yet.</p>
      ) : (
        <>
          <div
            className="dashboard-chart-plot"
            role="img"
            aria-label={`${title}. Values are in the table below.`}
          >
            <Suspense>
              <DashboardPlot
                points={points}
                unit={unit}
                maximum={maximum}
                maximumBarWidth={maximumBarWidth}
              />
            </Suspense>
          </div>
          <details>
            <summary>View values ({unit})</summary>
            <table>
              <caption>
                {title}, {unit}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Period or record</th>
                  <th scope="col">{unit}</th>
                </tr>
              </thead>
              <tbody>
                {points.map((point, index) => (
                  <tr key={index}>
                    <th scope="row">{point.label}</th>
                    <td>{formatValue(point.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </figure>
  );
}
