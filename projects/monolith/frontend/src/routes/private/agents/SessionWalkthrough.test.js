// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import SessionWalkthrough from "./SessionWalkthrough.svelte";
import pageSource from "./+page.svelte?raw";
import { vmState } from "./status.js";

const mounted = [];

function isProminent(session, vms, turnIndex, turnCount) {
  return vmState(session, vms) !== "awake" && turnIndex === turnCount - 1;
}

async function renderWalkthrough(prominent) {
  const target = document.createElement("div");
  document.body.append(target);
  const props = { sessionId: 17, turnSeq: 3, model: "luna" };
  if (prominent !== undefined) props.prominent = prominent;
  const component = mount(SessionWalkthrough, {
    target,
    props,
  });
  mounted.push({ component, target });
  await tick();
  return target.querySelector("details");
}

afterEach(async () => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("finished-session prominence", () => {
  const session = { ember_session_id: "vm-1" };

  test("the turn loop combines VM state with the last-turn check", () => {
    expect(pageSource).toContain(
      'prominent={vmState(selectedSession, vms) !== "awake" &&',
    );
    expect(pageSource).toContain(
      "turnIndex === (detail?.turns ?? []).length - 1}",
    );
  });

  test("opens the last turn when the VM is not awake and loads once", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ rung: 5, steps: [], stats: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const prominent = isProminent(
      session,
      { "vm-1": { state: "asleep" } },
      2,
      3,
    );
    const details = await renderWalkthrough(prominent);

    expect(details.open).toBe(true);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    details.dispatchEvent(new Event("toggle"));
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test.each([
    ["an omitted prominence prop", undefined, 0, 0],
    ["a non-last turn", { "vm-1": { state: "asleep" } }, 1, 3],
    [
      "the last turn while the VM is awake",
      { "vm-1": { state: "awake" } },
      2,
      3,
    ],
  ])("stays closed for %s", async (_label, vms, turnIndex, turnCount) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const prominent = vms
      ? isProminent(session, vms, turnIndex, turnCount)
      : undefined;
    const details = await renderWalkthrough(prominent);

    expect(details.open).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
