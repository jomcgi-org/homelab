import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { signIn } from "../app/signIn";

const browserStorage = localStorage;

afterEach(() => {
  cleanup();
  signIn.reset();
  browserStorage.clear();
});
