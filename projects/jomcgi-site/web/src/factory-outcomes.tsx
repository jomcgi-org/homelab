import snapshot from "./factory-outcomes.json";

type Rate = { value: number; low: number; high: number };
type Arm = {
  arm: string;
  n: number;
  merge_rate: Rate;
  revert_rate: Rate | null;
  median_rounds: number | null;
  median_minutes: number | null;
};

function percent(value: number) {
  return (value * 100).toFixed(1) + "%";
}

function interval(rate: Rate | null) {
  return rate
    ? `${percent(rate.value)} (${percent(rate.low)}–${percent(rate.high)})`
    : "—";
}

export function FactoryOutcomes({ arms = snapshot }: { arms?: Arm[] }) {
  return (
    <section className="table-wrap">
      <h2>Factory outcomes</h2>
      {arms.length === 0 ? (
        <p>Awaiting the first reviewed factory results.</p>
      ) : (
        <table>
          <caption>
            Delivery rates with 95% Wilson intervals. Revert rates cover merged
            PRs.
          </caption>
          <thead>
            <tr>
              {[
                "Factory arm",
                "PRs (n)",
                "Merged",
                "Reverted",
                "Median review rounds",
                "Median coder minutes per merge",
              ].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {arms.map((row) => (
              <tr key={row.arm}>
                <th scope="row">{row.arm}</th>
                <td>
                  {row.n}
                  {row.n < 10 && " · insufficient data"}
                </td>
                <td>{interval(row.merge_rate)}</td>
                <td>{interval(row.revert_rate)}</td>
                <td>{row.median_rounds ?? "—"}</td>
                <td>{row.median_minutes?.toFixed(1) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
