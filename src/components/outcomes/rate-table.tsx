import { rateText, type OutcomeGroup } from "@/lib/outcomes";
import { cn } from "@/lib/utils";

/**
 * Interview rate for one way of splitting the applications (#6): by source, by the review's
 * recommendation, by lane, or by resume. Each row gives its applications and how many reached
 * an interview; the rate only when Django sent one, which it does from ten applications. Under
 * that the row says so, because 1 of 3 is not 33%.
 */
export function RateTable({
  title,
  groups,
  name,
  className,
}: {
  title: string;
  groups: OutcomeGroup[];
  /** A group's key in words. */
  name: (key: string) => string;
  className?: string;
}) {
  return (
    <table className={cn("w-full text-[13.5px]", className)}>
      <caption className="sr-only">Interview rate {title.toLowerCase()}</caption>
      <thead>
        <tr className="text-muted-foreground border-b text-left text-[12.5px]">
          <th className="text-foreground py-2 pr-4 text-[13.5px] font-medium" scope="col">
            {title}
          </th>
          <th className="py-2 pr-4 text-right font-normal" scope="col">
            Applications
          </th>
          <th className="py-2 pr-4 text-right font-normal" scope="col">
            Interviews
          </th>
          <th className="py-2 text-right font-normal" scope="col">
            Rate
          </th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {groups.map((group) => (
          <tr key={group.key}>
            <th
              className={cn(
                "py-2.5 pr-4 text-left font-normal",
                group.applications === 0 && "text-muted-foreground",
              )}
              scope="row"
            >
              {name(group.key)}
            </th>
            <td className="tabular text-muted-foreground py-2.5 pr-4 text-right">
              {group.applications.toLocaleString()}
            </td>
            <td className="tabular text-muted-foreground py-2.5 pr-4 text-right">
              {group.interviews.toLocaleString()}
            </td>
            <td
              className={
                group.rate == null
                  ? "text-muted-foreground py-2.5 text-right"
                  : "tabular py-2.5 text-right font-medium"
              }
            >
              {rateText(group.rate, group.applications)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
