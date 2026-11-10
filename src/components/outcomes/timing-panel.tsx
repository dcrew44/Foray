import { Stat } from "@/components/foray/primitives";
import { plural } from "@/lib/format";
import { lagText, SPANS, timingNotes, type OutcomeTimings } from "@/lib/outcomes";

/**
 * Posted-to-applied time, the number Foray is judged by, and the three spans it is made of
 * (#65): the medians first, then each span's median and nine-in-ten time with the count of
 * applications behind it, over all time and over the last month.
 *
 * A span covers only the applications that have both of its ends: a posting with no date has
 * no posted time, and an application moved to Applied by hand has no ready time. The notes
 * under the table say how many of each there are.
 */
export function TimingPanel({ timing }: { timing: OutcomeTimings }) {
  const { allTime, recent, days, p90After } = timing;
  const notes = timingNotes(timing);

  if (allTime.applications === 0) {
    return (
      <div className="text-muted-foreground max-w-[75ch] space-y-1.5 text-[13.5px] leading-relaxed">
        <p>
          No application has a time sent yet, so there is nothing to time. Send one from the
          Apply page, choose I sent it, and its three spans appear here.
        </p>
        {notes.map((note) => (
          <p key={note}>{note}</p>
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div
        aria-label="Median times"
        className="bg-card grid gap-x-10 gap-y-5 rounded-xl border px-5 py-4 sm:grid-cols-2 lg:grid-cols-4"
        role="group"
      >
        {SPANS.map((span) => (
          <Stat
            key={span.key}
            label={
              <>
                <span className="text-foreground block">{span.label}</span>
                Median of {plural(allTime[span.key].applications, "application")}
              </>
            }
            value={lagText(allTime[span.key].medianS)}
          />
        ))}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] text-[13.5px]">
          <caption className="sr-only">
            Each span&apos;s median and nine-in-ten time, over all time and the last {days} days
          </caption>
          <thead className="text-muted-foreground text-[12.5px]">
            <tr>
              <td />
              <th className="border-b py-2 pr-4 text-left font-normal" colSpan={3} scope="colgroup">
                All time
              </th>
              <th className="border-b py-2 text-left font-normal" colSpan={3} scope="colgroup">
                Last {days} days
              </th>
            </tr>
            <tr className="border-b text-left">
              <th className="py-2 pr-4 font-normal" scope="col">
                Span
              </th>
              <th className="py-2 pr-4 font-normal" scope="col">
                Median
              </th>
              <th className="py-2 pr-4 font-normal" scope="col">
                Nine in ten within
              </th>
              <th className="py-2 pr-6 text-right font-normal" scope="col">
                Applications
              </th>
              <th className="py-2 pr-4 font-normal" scope="col">
                Median
              </th>
              <th className="py-2 pr-4 font-normal" scope="col">
                Nine in ten within
              </th>
              <th className="py-2 text-right font-normal" scope="col">
                Applications
              </th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {SPANS.map((span) => {
              const all = allTime[span.key];
              const late = recent[span.key];
              return (
                <tr key={span.key}>
                  <th className="py-2.5 pr-4 text-left font-normal" scope="row">
                    <span className="block">{span.label}</span>
                    <span className="text-muted-foreground block text-[12.5px]">{span.hint}</span>
                  </th>
                  <td className="tabular py-2.5 pr-4 font-medium">{lagText(all.medianS)}</td>
                  <td className="tabular text-muted-foreground py-2.5 pr-4">
                    {lagText(all.p90S)}
                  </td>
                  <td className="tabular text-muted-foreground py-2.5 pr-6 text-right">
                    {all.applications.toLocaleString()}
                  </td>
                  <td className="tabular py-2.5 pr-4 font-medium">{lagText(late.medianS)}</td>
                  <td className="tabular text-muted-foreground py-2.5 pr-4">
                    {lagText(late.p90S)}
                  </td>
                  <td className="tabular text-muted-foreground py-2.5 text-right">
                    {late.applications.toLocaleString()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="text-muted-foreground max-w-[75ch] space-y-1.5 text-[13px] leading-relaxed">
        <p>
          Measured on {plural(allTime.applications, "application")} with a time sent,{" "}
          {recent.applications.toLocaleString()} of them in the last {days} days. A nine-in-ten
          time appears once {p90After} applications have that span.
        </p>
        {notes.map((note) => (
          <p key={note}>{note}</p>
        ))}
      </div>
    </div>
  );
}
