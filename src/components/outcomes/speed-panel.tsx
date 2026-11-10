import { plural } from "@/lib/format";
import { BUCKET_HINT, BUCKET_LABEL, rateText, type OutcomeSpeed } from "@/lib/outcomes";

/**
 * Whether applying sooner helps, which is the claim Foray's direction rests on (#65):
 * applications by how long after the posting went up they were sent, and how many of each
 * reached an interview.
 *
 * Until enough applications are recorded Django sends no rates, and this shows the counts
 * and says how many more are needed: with a handful in each row, a difference between rows
 * is chance. Only applications marked sent on the Apply page are here: when a match was moved
 * to Applied by hand is when a card was moved, not when the application went in.
 */
export function SpeedPanel({ speed, minimum }: { speed: OutcomeSpeed; minimum: number }) {
  if (speed.applications === 0) {
    return (
      <p className="text-muted-foreground max-w-[75ch] text-[13.5px] leading-relaxed">
        Nothing to sort yet. This counts applications marked sent on the Apply page, to postings
        that say when they went up. Interview rates appear once {speed.needed} of them are
        recorded.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <table className="w-full text-[13.5px]">
        <caption className="sr-only">Applications and interviews by how soon they were sent</caption>
        <thead>
          <tr className="text-muted-foreground border-b text-left text-[12.5px]">
            <th className="py-2 pr-4 font-normal" scope="col">
              Sent
            </th>
            <th className="py-2 pr-4 text-right font-normal" scope="col">
              Applications
            </th>
            <th className="py-2 pr-4 text-right font-normal" scope="col">
              Reached an interview
            </th>
            {speed.enough && (
              <th className="py-2 text-right font-normal" scope="col">
                Interview rate
              </th>
            )}
          </tr>
        </thead>
        <tbody className="divide-y">
          {speed.buckets.map((bucket) => (
            <tr key={bucket.bucket}>
              <th className="py-2.5 pr-4 text-left font-normal" scope="row">
                <span className="block">{BUCKET_LABEL[bucket.bucket]}</span>
                <span className="text-muted-foreground block text-[12.5px]">
                  {BUCKET_HINT[bucket.bucket]}
                </span>
              </th>
              <td className="tabular py-2.5 pr-4 text-right">
                {bucket.applications.toLocaleString()}
              </td>
              <td className="tabular py-2.5 pr-4 text-right">
                {bucket.interviews.toLocaleString()}
              </td>
              {speed.enough && (
                <td
                  className={
                    bucket.rate == null
                      ? "text-muted-foreground py-2.5 text-right"
                      : "tabular py-2.5 text-right font-medium"
                  }
                >
                  {rateText(bucket.rate, bucket.applications)}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>

      <p className="text-muted-foreground max-w-[75ch] text-[13px] leading-relaxed">
        {speed.enough
          ? `Rates rest on ${plural(speed.applications, "application")} marked sent on the Apply page. A row with fewer than ${minimum} shows its counts only.`
          : `${speed.applications.toLocaleString()} of the ${speed.needed} applications needed before a rate means anything. Until then these are counts: with so few in each row, a difference between them is as likely to be chance.`}{" "}
        Applications moved to Applied by hand are left out: when you moved the match is not
        when the application went in.
      </p>
    </div>
  );
}
