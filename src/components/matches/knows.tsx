import { Users } from "lucide-react";

import type { components } from "@/lib/api/schema";
import { knowsLabel } from "@/lib/contacts";
import { cn } from "@/lib/utils";

type MatchKnows = components["schemas"]["MatchKnows"];

/**
 * That the user knows someone at this posting's company, and who (#64). Django says it for
 * every row a page shows (`matching.queries.with_known`), from the contacts the user imported,
 * so the list, the board, Home and a run's results agree.
 *
 * A posting where they know nobody shows nothing. Said in ink, not in colour: marigold means
 * agents working, and this is a fact about the user's own contacts. Server-safe, and a span:
 * it sits inside links.
 */
export function KnowsMark({
  knows,
  className,
}: {
  knows: MatchKnows | null | undefined;
  className?: string;
}) {
  const label = knowsLabel(knows);
  if (!label || !knows) return null;
  const everyone = knows.people
    .map((person) => (person.position ? `${person.name} (${person.position})` : person.name))
    .join(", ");
  const more = knows.count - knows.people.length;
  return (
    <span
      className={cn(
        "text-foreground/85 flex min-w-0 items-center gap-1.5 text-[12.5px] leading-snug",
        className,
      )}
      title={more > 0 ? `${everyone}, and ${more} more` : everyone}
    >
      <Users aria-hidden className="size-[1em] shrink-0 opacity-70" />
      <span className="min-w-0 truncate">{label}</span>
    </span>
  );
}
