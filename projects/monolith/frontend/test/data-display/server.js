import { render } from "svelte/server";
import {
  Panel,
  KeyValue,
  Status,
  Metric,
  ChartFrame,
  Legend,
} from "@homelab/design-system/data-display";
import Fixture from "./Fixture.svelte";
import ChartHarness from "./ChartHarness.svelte";

export function renderDisplayFixture(props = {}) {
  return render(Fixture, { props });
}

export function renderDisplayComponent(name, props) {
  return render(
    { Panel, KeyValue, Status, Metric, ChartFrame, Legend, ChartHarness }[name],
    { props },
  );
}
