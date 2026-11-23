/** Holds the place of content that is loading, so nothing shifts when it
 * arrives. It says nothing to assistive technology. */
export function Skeleton({
  size = "block",
}: {
  /** `block`: a short section. `map`: the height of a map. */
  size?: "block" | "map";
}) {
  return <div className="ui-skeleton" data-size={size} aria-hidden="true" />;
}
