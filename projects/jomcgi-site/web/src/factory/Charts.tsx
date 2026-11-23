import { lazy, Suspense, useId } from "react";
import type { BurnPoint } from "../../../../factory-storyboard/schema/types";

const BurnUpPlot = lazy(() => import("./BurnUpPlot"));

export function BurnUp({ points }: { points: BurnPoint[] }) {
  const id = useId();
  const last = points.at(-1);
  return (
    <figure className="chart burn-up" aria-labelledby={id}>
      <figcaption id={id}>Goal burn-up</figcaption>
      {!last ? (
        <p>No measurements yet.</p>
      ) : (
        <>
          <div
            className="burn-up-plot"
            role="img"
            aria-label="Shipped issues and total scope over time. Values are in the table below."
          >
            <Suspense>
              <BurnUpPlot points={points} />
            </Suspense>
          </div>
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
