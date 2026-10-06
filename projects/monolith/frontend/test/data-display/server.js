import { render } from "svelte/server";
import {
  Panel,
  KeyValue,
  Status,
  Metric,
} from "@homelab/design-system/data-display";
import Fixture from "./Fixture.svelte";

export function renderDisplayFixture(props = {}) {
  return render(Fixture, { props });
}

export function renderDisplayComponent(name, props) {
  return render({ Panel, KeyValue, Status, Metric }[name], { props });
}
