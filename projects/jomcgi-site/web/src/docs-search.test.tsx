import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DocsSearch } from "./docs-search";

const loaded = vi.hoisted(() => vi.fn());
vi.mock("./docs-search-index", () => {
  loaded();
  return {
    search: () => [
      { slug: "embervm", title: "EmberVM", project: "embervm", kind: "readme" },
    ],
  };
});

it("does not import the search index until intent", async () => {
  render(<DocsSearch />);
  expect(loaded).not.toHaveBeenCalled();
  const input = screen.getByRole("searchbox");
  fireEvent.focus(input);
  await waitFor(() => expect(loaded).toHaveBeenCalledTimes(1));
  fireEvent.change(input, { target: { value: "ember" } });
  expect(screen.getByRole("link").getAttribute("href")).toBe("/docs/embervm");
});

it("Enter opens the first result without moving focus from the search box", async () => {
  render(<DocsSearch />);
  const input = screen.getByRole("searchbox");
  fireEvent.change(input, { target: { value: "ember" } });
  const result = await screen.findByRole("link");
  const click = vi.fn((event: MouseEvent) => event.preventDefault());
  result.addEventListener("click", click);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(click).toHaveBeenCalledOnce();
  expect(result.getAttribute("href")).toBe("/docs/embervm");

  // Enter while an IME is composing must not navigate.
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  expect(click).toHaveBeenCalledOnce();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(click).toHaveBeenCalledOnce();
});
