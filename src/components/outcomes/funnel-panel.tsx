import { STAGE_LABEL } from "@/lib/format";
import { barWidth, NO_NUMBER, rateText, type OutcomeFunnel } from "@/lib/outcomes";

/**
 * How far the user's matches have got (#6): each step with the matches known to have reached
 * it or gone past it, and the share of the step before. Rejected and archived matches are
 * outcomes rather than steps, so they are listed apart, with how many of them were applied to
 * and how many reached an interview before they closed.
 *
 * The bars compare each step with the first on one pine scale. A step that is not empty always
 * shows a sliver: one application among hundreds of matches is the point, not a rounding error.
 */
export function FunnelPanel({ funnel }: { funnel: OutcomeFunnel }) {
  const most = funnel.steps[0]?.matches ?? 0;

  return (
    <div className="space-y-6">
      <table className="w-full text-[13.5px]">
        <caption className="sr-only">Matches that reached each step</caption>
        <thead>
          <tr className="text-muted-foreground border-b text-left text-[12.5px]">
            <th className="py-2 pr-4 font-normal" scope="col">
              Step
            </th>
            <th className="hidden w-[40%] py-2 pr-4 font-normal sm:table-cell">
              <span className="sr-only">Share of all matches</span>
            </th>
            <th className="py-2 pr-4 text-right font-normal" scope="col">
              Matches
            </th>
            <th className="py-2 text-right font-normal" scope="col">
              Of the step before
            </th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {funnel.steps.map((step, index) => {
            const before = funnel.steps[index - 1]?.matches ?? 0;
            return (
              <tr key={step.stage}>
                <th className="py-2.5 pr-4 text-left font-normal" scope="row">
                  {STAGE_LABEL[step.stage]}
                </th>
                <td className="hidden py-2.5 pr-4 sm:table-cell">
                  <span className="bg-muted block h-1.5 overflow-hidden rounded-full">
                    <span
                      className="bg-primary block h-full rounded-full"
                      style={{ width: `${barWidth(step.matches, most)}%` }}
                    />
                  </span>
                </td>
                <td className="tabular py-2.5 pr-4 text-right font-medium">
                  {step.matches.toLocaleString()}
                </td>
                <td
                  className={
                    step.rate == null
                      ? "text-muted-foreground py-2.5 text-right"
                      : "tabular py-2.5 text-right"
                  }
                >
                  {index === 0 ? NO_NUMBER : rateText(step.rate, before)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <table className="w-full text-[13.5px]">
        <caption className="sr-only">Closed matches</caption>
        <thead>
          <tr className="text-muted-foreground border-b text-left text-[12.5px]">
            <th className="py-2 pr-4 font-normal" scope="col">
              Closed
            </th>
            <th className="py-2 pr-4 text-right font-normal" scope="col">
              Matches
            </th>
            <th className="py-2 pr-4 text-right font-normal" scope="col">
              Applied to
            </th>
            <th className="py-2 text-right font-normal" scope="col">
              Interviewed
            </th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {funnel.closed.map((closed) => (
            <tr key={closed.stage}>
              <th className="py-2.5 pr-4 text-left font-normal" scope="row">
                {STAGE_LABEL[closed.stage]}
              </th>
              <td className="tabular py-2.5 pr-4 text-right font-medium">
                {closed.matches.toLocaleString()}
              </td>
              <td className="tabular text-muted-foreground py-2.5 pr-4 text-right">
                {closed.applied.toLocaleString()}
              </td>
              <td className="tabular text-muted-foreground py-2.5 text-right">
                {closed.interviewed.toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="text-muted-foreground max-w-[75ch] text-[13px] leading-relaxed">
        A match counts at every step it reached, wherever it is now: one that was interviewed
        and then rejected is still counted at Interviewing. A share is shown once the step
        before has {funnel.minDenominator} matches.
      </p>
    </div>
  );
}
