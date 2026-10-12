import type { Metadata } from "next";

import { PageBody, PageHeader, Section, SignalDot, Stat } from "@/components/foray/primitives";
import { PageNav } from "@/components/profile/page-nav";
import { BehaviourForm } from "@/components/settings/behaviour-form";
import { AppearanceForm } from "@/components/settings/appearance-form";
import { SignOutButton } from "@/components/settings/sign-out-button";
import {
  AggregatorMeter,
  ModelTable,
  monthDays,
  PROVIDER_LABEL,
  PURPOSE_LABEL,
  PurposeTable,
  SpendChart,
} from "@/components/settings/spend";
import { WorkerPanel } from "@/components/settings/worker-panel";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { formatUsd, initials } from "@/lib/format";

export const metadata: Metadata = { title: "Settings" };

type S = components["schemas"];
type ReasoningEffort = S["PurposeModel"]["reasoning"];
type ProviderId = S["ProviderKeys"]["present"][number];

const EFFORT_LABEL: Record<ReasoningEffort, string> = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
};

const PROVIDERS: ProviderId[] = ["openai", "anthropic", "google", "deepseek", "compat"];

/**
 * How the agents behave for the user, what they have cost this month, what
 * they run on, and whether the worker is up. One read of Django's settings
 * page; provider keys never leave the server, only which ones are set.
 */
export default async function SettingsPage() {
  const page = await django<S["SettingsPage"]>("/api/pages/settings");
  const { user, settings, spend: total, byPurpose, byModel, byDay, aggregator, purposes } = page;
  const since = new Date(page.monthStart);

  // The same estimator the approval card quotes from, so the two always agree.
  const review = page.review;
  const present = new Set<string>(page.providers.present);
  const inUse = new Set(purposes.flatMap((row) => (row.ref ? [row.ref.split(":")[0]] : [])));

  const monthName = since.toLocaleDateString("en-US", { month: "long", timeZone: "UTC" });
  const nextMonth = new Date(Date.UTC(since.getUTCFullYear(), since.getUTCMonth() + 1, 1));
  const resetsOn = nextMonth.toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });

  return (
    <PageBody>
      <PageHeader
        description="How the agents behave for you, what they have cost, and what they run on."
        title="Settings"
      />

      <div className="mt-10 xl:grid xl:grid-cols-[minmax(0,1fr)_13rem] xl:gap-16">
        <div className="min-w-0 max-w-3xl space-y-16">
          <div className="scroll-mt-20 lg:scroll-mt-8" id="behaviour">
            <Section
              description="Defaults for every search. Changes apply to the next one."
              title="How Foray works for you"
            >
              <BehaviourForm
                initial={settings}
                reviewCost={review.perItemUsd}
                reviewModel={review.perItemUsd != null ? (review.model.split(":").pop() ?? null) : null}
              />
            </Section>
          </div>

          <div className="scroll-mt-20 lg:scroll-mt-8" id="usage">
            <Section
              description={`Every model call is recorded with its cost. Counted from ${monthName} 1, in UTC.`}
              title="Spend this month"
            >
              <div className="space-y-8">
                <div className="grid grid-cols-2 gap-6 sm:grid-cols-3">
                  <Stat label={`Spent in ${monthName}`} value={formatUsd(total.usd, { precise: true })} />
                  <Stat label="Model calls" value={total.calls.toLocaleString()} />
                  <Stat
                    className="col-span-2 sm:col-span-1"
                    label="Tokens read and written"
                    value={`${compact(total.inputTokens)} / ${compact(total.outputTokens)}`}
                  />
                </div>

                {total.calls === 0 ? (
                  <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
                    Nothing spent yet this month. Searches, reviews and chat replies show up
                    here as they run.
                  </p>
                ) : (
                  <>
                    <SpendChart days={monthDays(since, byDay)} />

                    <div className="space-y-2">
                      <h3 className="text-[14px] font-semibold">By purpose</h3>
                      <PurposeTable rows={byPurpose} />
                    </div>

                    <div className="space-y-2">
                      <h3 className="text-[14px] font-semibold">By model</h3>
                      <ModelTable rows={byModel} />
                    </div>
                  </>
                )}

                <div className="border-t pt-6">
                  <AggregatorMeter
                    budget={aggregator.budget}
                    enabled={aggregator.enabled}
                    resetsOn={resetsOn}
                    used={aggregator.used}
                  />
                </div>
              </div>
            </Section>
          </div>

          <div className="scroll-mt-20 lg:scroll-mt-8" id="models">
            <Section
              description={
                <>
                  Chosen in <code className="text-foreground">.env</code>:{" "}
                  <code className="text-foreground">LLM_DEFAULT</code> for everything, or{" "}
                  <code className="text-foreground">LLM_SCORE</code>,{" "}
                  <code className="text-foreground">LLM_EVALUATE</code> and the rest for one
                  purpose. Restart the app and worker after a change.
                </>
              }
              title="Models"
            >
              <div className="space-y-5">
                <div className="overflow-x-auto rounded-xl border">
                  <table className="w-full min-w-[34rem] text-[13.5px]">
                    <caption className="sr-only">Model used for each purpose</caption>
                    <thead>
                      <tr className="text-muted-foreground border-b text-left text-[12.5px]">
                        <th className="py-2.5 pr-4 pl-5 font-normal">Purpose</th>
                        <th className="py-2.5 pr-4 font-normal">Model</th>
                        <th className="py-2.5 pr-4 font-normal">Reasoning</th>
                        <th className="py-2.5 pr-5 text-right font-normal">
                          Per million tokens
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {purposes.map((row) => {
                        const [provider, model] = row.ref?.split(":") ?? [];
                        const rate = row.rate;
                        return (
                          <tr className="align-top" key={row.purpose}>
                            <td className="py-3 pr-4 pl-5 font-medium">
                              {PURPOSE_LABEL[row.purpose] ?? row.purpose}
                            </td>
                            {row.ref ? (
                              <>
                                <td className="py-3 pr-4">
                                  <span className="block">{model}</span>
                                  <span className="text-muted-foreground block text-[12.5px]">
                                    {PROVIDER_LABEL[provider ?? ""] ?? provider}
                                  </span>
                                </td>
                                <td className="text-muted-foreground py-3 pr-4">
                                  {EFFORT_LABEL[row.reasoning]}
                                </td>
                                <td className="tabular text-muted-foreground py-3 pr-5 text-right">
                                  {rate ? priceLabel(rate) : "Not priced"}
                                </td>
                              </>
                            ) : (
                              <td className="text-destructive py-3 pr-5 text-[13px]" colSpan={3}>
                                {row.error}
                              </td>
                            )}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="space-y-2">
                  <h3 className="text-[14px] font-semibold">Provider keys</h3>
                  <ul className="flex flex-wrap gap-2">
                    {PROVIDERS.map((id) => (
                      <li
                        className="inline-flex h-8 items-center gap-2 rounded-full border px-3 text-[13px]"
                        key={id}
                      >
                        <SignalDot tone={present.has(id) ? "ok" : "idle"} />
                        <span className={present.has(id) ? "" : "text-muted-foreground"}>
                          {PROVIDER_LABEL[id]}
                        </span>
                        <span className="text-muted-foreground text-[12.5px]">
                          {present.has(id)
                            ? inUse.has(id)
                              ? "in use"
                              : id === "compat"
                                ? "configured"
                                : "key set"
                            : "not set"}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p className="text-muted-foreground text-[12.5px]">
                    Keys stay on the server and are never shown here. Reasoning effort can be
                    tuned per purpose too, for example{" "}
                    <code className="text-foreground">LLM_REASONING_SCORE=none</code>.
                  </p>
                </div>
              </div>
            </Section>
          </div>

          <div className="scroll-mt-20 lg:scroll-mt-8" id="worker">
            <Section
              description="The background process that runs searches, reviews and tailoring. The app only queues the work."
              title="Worker"
            >
              <WorkerPanel />
            </Section>
          </div>

          <div className="scroll-mt-20 lg:scroll-mt-8" id="appearance">
            <Section
              description="Saved in this browser, and applied the moment you pick it."
              title="Appearance"
            >
              <AppearanceForm />
            </Section>
          </div>

          <div className="scroll-mt-20 lg:scroll-mt-8" id="account">
            <Section title="Account">
              <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border px-5 py-4">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="bg-primary/10 text-primary text-display inline-flex size-10 shrink-0 items-center justify-center rounded-full text-[14px]">
                    {initials(user.name || user.email)}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-[14px] font-medium">{user.name || "No name set"}</p>
                    <p className="text-muted-foreground truncate text-[13px]">{user.email}</p>
                  </div>
                </div>
                <SignOutButton />
              </div>
            </Section>
          </div>
        </div>

        <aside className="hidden xl:block">
          <PageNav
            className="sticky top-10"
            items={[
              { id: "behaviour", label: "How Foray works for you" },
              { id: "usage", label: "Spend this month", detail: formatUsd(total.usd) },
              { id: "models", label: "Models" },
              { id: "worker", label: "Worker" },
              { id: "appearance", label: "Appearance" },
              { id: "account", label: "Account" },
            ]}
          />
        </aside>
      </div>
    </PageBody>
  );
}

function priceLabel(rate: S["TokenRate"]): string {
  const usd = (n: number) => (Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`);
  return `${usd(rate.input)} in, ${usd(rate.output)} out`;
}

/** 1,234 → "1.2k", 3,400,000 → "3.4M". */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return String(Math.round(n));
}
