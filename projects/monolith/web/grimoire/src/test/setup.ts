import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { signIn } from "../app/signIn";

afterEach(() => {
  cleanup();
  signIn.reset();
});
