import type { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { Button } from "@astryxdesign/core/Button";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { VStack } from "@astryxdesign/core/Layout";
import { errorMessage, SignInError } from "../api/client";

interface LoadedProps<T> {
  query: UseQueryResult<T>;
  /** What failed to load, for the error title: "your campaigns". */
  what: string;
  children: (data: T) => ReactNode;
}

/**
 * Renders a query's three states the same way everywhere: a skeleton while
 * loading, a retry prompt on failure, the content once there is data. Data
 * already on screen stays there while a background refresh fails.
 */
export function Loaded<T>({ query, what, children }: LoadedProps<T>) {
  if (query.data !== undefined) return <>{children(query.data)}</>;
  if (query.isError && !(query.error instanceof SignInError)) {
    return (
      <EmptyState
        isCompact
        title={`Could not load ${what}`}
        description={errorMessage(query.error)}
        actions={<Button label="Try again" onClick={() => query.refetch()} />}
      />
    );
  }
  return (
    <VStack gap={2} aria-busy="true">
      <Skeleton width="60%" height={20} />
      <Skeleton width="100%" height={20} />
      <Skeleton width="80%" height={20} />
    </VStack>
  );
}
