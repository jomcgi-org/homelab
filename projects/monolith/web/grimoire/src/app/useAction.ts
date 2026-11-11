import {
  useMutation,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { useToast } from "@astryxdesign/core/Toast";
import { errorMessage, SignInError } from "../api/client";

interface ActionOptions<TInput, TResult> {
  /** Queries to refetch once the change is saved. */
  invalidate?: QueryKey[];
  /** Confirmation toast. Omit when the change is visible on its own. */
  success?: string | ((result: TResult, input: TInput) => string);
  onSuccess?: (result: TResult, input: TInput) => void;
}

/**
 * A write to the backend: runs it, refetches what it changed, and reports the
 * outcome with a toast. Every button that saves something uses this, so
 * feedback and error wording are the same across the app.
 *
 *   const invite = useAction(invitePlayer, {invalidate: [keys.lobby], success: 'Invited.'});
 *   <Button label="Invite" isLoading={invite.isPending} onClick={() => invite.run({email})} />
 */
export function useAction<TInput, TResult>(
  action: (input: TInput) => Promise<TResult>,
  options: ActionOptions<TInput, TResult> = {},
) {
  const client = useQueryClient();
  const toast = useToast();
  const mutation = useMutation({
    mutationFn: action,
    onSuccess: async (result, input) => {
      await Promise.all(
        (options.invalidate ?? []).map((queryKey) =>
          client.invalidateQueries({ queryKey }),
        ),
      );
      const message =
        typeof options.success === "function"
          ? options.success(result, input)
          : options.success;
      if (message) toast({ body: message });
      options.onSuccess?.(result, input);
    },
    onError: (error) => {
      // The sign-in notice already explains an expired session.
      if (error instanceof SignInError) return;
      toast({ body: errorMessage(error), type: "error" });
    },
  });
  return {
    /** Resolves to the result, or undefined when the action failed. */
    run: (input: TInput) => mutation.mutateAsync(input).catch(() => undefined),
    isPending: mutation.isPending,
  };
}
