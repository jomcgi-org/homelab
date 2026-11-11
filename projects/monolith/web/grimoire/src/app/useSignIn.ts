import { useSyncExternalStore } from "react";
import { signIn, type SignInState } from "./signIn";

export function useSignIn(): SignInState {
  return useSyncExternalStore(signIn.subscribe, signIn.state);
}
