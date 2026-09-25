import { CircleAlert, CircleCheck, CircleDashed, Quote } from "lucide-react";

import { RecommendationPill, ScoreChip } from "@/components/foray/primitives";
import { fitWord } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Why a match fits, as the two scoring passes judged it. Server-rendered: the
 * evaluation is data at rest, and nothing here needs the client.
 */

type Requirement = {
  requirement: string;
  evidence: string;
  fact_ids?: string[];
  strength?: "strong" | "partial";
};
type Gap = { gap: string; severity: "learnable" | "disqualifying"; note?: string };

export type DeepEvalView = {
  score?: number;
  recommendation?: string;
  summary?: string;
  matched_requirements?: Requirement[];
  gaps?: Gap[];
  seniority_fit?: string;
  location_fit?: string;
  salary_fit?: string;
  risk_flags?: string[];
  talking_points?: string[];
};

export type FastEvalView = {
  score?: number;
  fit_reasons?: string[];
  red_flags?: string[];
  seniority?: string;
  hard_requirement_misses?: string[];
};

const FIT_WORDS: Record<string, Record<string, { label: string; tone: "good" | "warn" | "bad" | "neutral" }>> = {
  seniority_fit: {
    match: { label: "Right level", tone: "good" },
    under: { label: "Above your level", tone: "warn" },
    over: { label: "Below your level", tone: "warn" },
    unclear: { label: "Level unclear", tone: "neutral" },
  },
  location_fit: {
    match: { label: "Location works", tone: "good" },
    partial: { label: "Location partly works", tone: "warn" },
    mismatch: { label: "Location doesn't fit", tone: "bad" },
    unclear: { label: "Location unclear", tone: "neutral" },
  },
  salary_fit: {
    above: { label: "Pays above your floor", tone: "good" },
    within: { label: "Pay meets your floor", tone: "good" },
    below: { label: "Pay below your floor", tone: "bad" },
    unstated: { label: "Pay not stated", tone: "neutral" },
  },
};

/**
 * Models sometimes leave the resume's bracketed fact ids in prose meant for
 * people ("… dropped events by 94% [3d98…]"). They are for the machine.
 */
function clean(text: string | undefined | null): string {
  return (text ?? "").replace(/\s*\[[0-9a-f]{8}-[0-9a-f-]{27,}\]/gi, "").trim();
}

const TONE_CLASS = {
  good: "bg-fit-2 text-fit-2-foreground",
  warn: "bg-signal-soft text-signal-foreground",
  bad: "bg-destructive/10 text-destructive",
  neutral: "bg-muted text-muted-foreground",
};

export function DeepEvaluation({ evaluation }: { evaluation: DeepEvalView }) {
  const fits = (["seniority_fit", "location_fit", "salary_fit"] as const)
    .map((key) => {
      const value = evaluation[key];
      return value ? FIT_WORDS[key][value] : undefined;
    })
    .filter(Boolean) as { label: string; tone: keyof typeof TONE_CLASS }[];

  const requirements = evaluation.matched_requirements ?? [];
  const gaps = evaluation.gaps ?? [];

  return (
    <div className="space-y-8">
      <div className="flex items-start gap-5">
        <ScoreChip evaluated score={evaluation.score ?? null} size="xl" />
        <div className="min-w-0 space-y-2 pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-display text-[20px]">{fitWord(evaluation.score)}</span>
            <RecommendationPill value={evaluation.recommendation} />
          </div>
          {evaluation.summary && (
            <p className="text-foreground/85 max-w-[68ch] text-[14.5px] leading-relaxed">
              {clean(evaluation.summary)}
            </p>
          )}
          {fits.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {fits.map((fit) => (
                <span
                  className={cn("inline-flex h-6 items-center rounded-full px-2.5 text-[12px] font-medium", TONE_CLASS[fit.tone])}
                  key={fit.label}
                >
                  {fit.label}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {requirements.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-[14px] font-semibold">What lines up</h3>
          <ul className="divide-y rounded-xl border">
            {requirements.map((item, index) => (
              <li className="flex gap-3 px-4 py-3" key={index}>
                {item.strength === "partial" ? (
                  <CircleDashed className="text-fit-3 mt-0.5 size-4 shrink-0" />
                ) : (
                  <CircleCheck className="text-success mt-0.5 size-4 shrink-0" />
                )}
                <div className="min-w-0 space-y-1">
                  <p className="text-[13.5px] font-medium">{clean(item.requirement)}</p>
                  {item.evidence && (
                    <p className="text-muted-foreground flex gap-1.5 text-[13px] leading-snug">
                      <Quote className="mt-0.5 size-3 shrink-0 opacity-60" />
                      <span>{clean(item.evidence)}</span>
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {gaps.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-[14px] font-semibold">Gaps</h3>
          <ul className="divide-y rounded-xl border">
            {gaps.map((gap, index) => (
              <li className="flex gap-3 px-4 py-3" key={index}>
                <CircleAlert
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    gap.severity === "disqualifying" ? "text-destructive" : "text-signal",
                  )}
                />
                <div className="min-w-0 space-y-0.5">
                  <p className="text-[13.5px] font-medium">
                    {clean(gap.gap)}
                    <span className="text-muted-foreground ml-2 text-[12px] font-normal">
                      {gap.severity === "disqualifying" ? "Likely a blocker" : "Learnable"}
                    </span>
                  </p>
                  {gap.note && <p className="text-muted-foreground text-[13px] leading-snug">{clean(gap.note)}</p>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {((evaluation.talking_points?.length ?? 0) > 0 || (evaluation.risk_flags?.length ?? 0) > 0) && (
        <div className="grid gap-6 sm:grid-cols-2">
          {(evaluation.talking_points?.length ?? 0) > 0 && (
            <div className="space-y-2">
              <h3 className="text-[14px] font-semibold">What to lead with</h3>
              <ul className="text-foreground/85 list-disc space-y-1.5 pl-4 text-[13.5px] leading-snug">
                {evaluation.talking_points!.map((point, index) => (
                  <li key={index}>{clean(point)}</li>
                ))}
              </ul>
            </div>
          )}
          {(evaluation.risk_flags?.length ?? 0) > 0 && (
            <div className="space-y-2">
              <h3 className="text-[14px] font-semibold">Worth checking</h3>
              <ul className="text-foreground/85 list-disc space-y-1.5 pl-4 text-[13.5px] leading-snug">
                {evaluation.risk_flags!.map((flag, index) => (
                  <li key={index}>{clean(flag)}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function FastEvaluation({ evaluation }: { evaluation: FastEvalView }) {
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-5">
        <ScoreChip score={evaluation.score ?? null} size="xl" />
        <div className="min-w-0 space-y-1 pt-1">
          <p className="text-display text-[20px]">{fitWord(evaluation.score)}</p>
          <p className="text-muted-foreground max-w-[60ch] text-[13.5px] leading-relaxed">
            From the quick triage pass. A deep review reads the whole posting against your
            resume and quotes the evidence for every requirement.
          </p>
        </div>
      </div>
      <div className="grid gap-6 sm:grid-cols-2">
        {(evaluation.fit_reasons?.length ?? 0) > 0 && (
          <div className="space-y-2">
            <h3 className="text-[14px] font-semibold">Why it might fit</h3>
            <ul className="space-y-2">
              {evaluation.fit_reasons!.map((reason, index) => (
                <li className="flex gap-2 text-[13.5px] leading-snug" key={index}>
                  <CircleCheck className="text-success mt-0.5 size-4 shrink-0" />
                  {clean(reason)}
                </li>
              ))}
            </ul>
          </div>
        )}
        {((evaluation.red_flags?.length ?? 0) > 0 || (evaluation.hard_requirement_misses?.length ?? 0) > 0) && (
          <div className="space-y-2">
            <h3 className="text-[14px] font-semibold">Concerns</h3>
            <ul className="space-y-2">
              {evaluation.hard_requirement_misses?.map((miss, index) => (
                <li className="flex gap-2 text-[13.5px] leading-snug" key={`miss-${index}`}>
                  <CircleAlert className="text-destructive mt-0.5 size-4 shrink-0" />
                  {miss}
                </li>
              ))}
              {evaluation.red_flags?.map((flag, index) => (
                <li className="flex gap-2 text-[13.5px] leading-snug" key={`flag-${index}`}>
                  <CircleAlert className="text-signal mt-0.5 size-4 shrink-0" />
                  {flag}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
