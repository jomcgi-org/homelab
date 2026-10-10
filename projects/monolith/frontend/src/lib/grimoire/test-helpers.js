import { tick } from "svelte";

export async function settle(times = 8) {
  for (let step = 0; step < times; step += 1) await tick();
}

// happy-dom resolves a bound <select> through `:checked`, which it does not
// implement for options, so pick the option the way a browser would report it.
export async function chooseOption(select, value) {
  const option = [...select.options].find((item) => item.value === value);
  select.value = value;
  select.querySelector = (query) =>
    query === ":checked" ? option : Element.prototype.querySelector.call(select, query);
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
}

export async function setChecked(input, checked = true) {
  input.checked = checked;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
}

export async function typeInto(input, value) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await settle();
}

export const buttonByText = (root, text) =>
  [...root.querySelectorAll("button")].find(
    (button) => button.textContent.trim() === text,
  );

// Reads what a rendered projection shows: the field keys and the text.
export function renderedFields(root) {
  return [...root.querySelectorAll("[data-field]")].map(
    (node) => node.dataset.field,
  );
}

// Markdown renders emphasis away; compare on the plain words.
export const plain = (value) => String(value).replaceAll("*", "");
