import { useId } from "react";
import type { BurnPoint } from "../../../../factory-storyboard/schema/types";

export function BurnUp({ points }: { points: BurnPoint[] }) {
  const id = useId();
  const max = Math.max(
    1,
    ...points.flatMap((point) => [point.shipped, point.total]),
  );
  const coordinates = (field: "shipped" | "total") =>
    points
      .map(
        (point, index) =>
          `${36 + (index / Math.max(1, points.length - 1)) * 300},${140 - (point[field] / max) * 120}`,
      )
      .join(" ");
  return (
    <figure className="chart" aria-labelledby={id}>
      <figcaption id={id}>Goal burn-up</figcaption>
      {points.length === 0 ? (
        <p>No measurements yet.</p>
      ) : (
        <>
          <p>
            {points.at(-1)!.shipped} shipped of {points.at(-1)!.total} issues.
            Solid line: shipped. Dashed line: total scope.
          </p>
          <svg
            className="burn-up-plot"
            viewBox="0 0 340 160"
            role="img"
            aria-label="Shipped issues and total scope over time. Values are in the table below."
          >
            <text x="4" y="14">
              {max}
            </text>
            <text x="4" y="142">
              0
            </text>
            <line x1="36" y1="140" x2="336" y2="140" className="axis" />
            <polyline points={coordinates("total")} className="scope-line" />
            <polyline
              points={coordinates("shipped")}
              className="shipped-line"
            />
            {points.map((point, index) => (
              <circle
                key={index}
                className="bar"
                r="3"
                cx={36 + (index / Math.max(1, points.length - 1)) * 300}
                cy={140 - (point.shipped / max) * 120}
              />
            ))}
          </svg>
          <details>
            <summary>View burn-up values</summary>
            <table>
              <caption>Goal burn-up, issues</caption>
              <thead>
                <tr>
                  <th scope="col">Date</th>
                  <th scope="col">Shipped</th>
                  <th scope="col">Scope</th>
                </tr>
              </thead>
              <tbody>
                {points.map((point, index) => (
                  <tr key={index}>
                    <th scope="row">{point.date}</th>
                    <td>{point.shipped}</td>
                    <td>{point.total}</td>
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
