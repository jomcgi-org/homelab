import { lazy, Suspense, useId } from "react";
import "./dashboard-chart.css";

export type ChartPoint = { label: string; value: number };

const DashboardPlot = lazy(() => import("./DashboardPlot"));

/** A titled column chart with its values as a table. */
export function DashboardChart({
  title,
  headline,
  note,
  unit,
  category = "Period",
  points,
  maximum,
  slots,
  formatValue,
  formatLabel,
}: {
  title: string;
  /** One sentence: what the chart shows. */
  headline?: string;
  /** One short line under the title, such as a median. */
  note?: string;
  unit: string;
  /** The table heading for the labels. */
  category?: string;
  points: readonly ChartPoint[];
  maximum?: number;
  /** The fewest column slots. A shorter series leaves the rest empty. */
  slots?: number;
  formatValue: (value: number) => string;
  /** A shorter label for the axis. The table keeps the full label. */
  formatLabel?: (label: string) => string;
}) {
  const id = useId();

  return (
    <figure className="chart" aria-labelledby={id}>
      <figcaption id={id}>{title}</figcaption>
      {headline && <p className="chart-headline">{headline}</p>}
      {note && <p className="chart-note">{note}</p>}
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
                slots={slots}
                formatValue={formatValue}
                formatLabel={formatLabel}
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
                  <th scope="col">{category}</th>
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
