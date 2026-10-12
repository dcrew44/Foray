"use client";

import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { SaveBar, useUnsavedChanges } from "@/components/profile/form-controls";
import { Slider } from "@/components/ui/slider";
import { api, call } from "@/lib/api/client";
import type { Settings } from "@/lib/api/types";
import { formatUsd } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * How the agents behave by default: whether a search stops to ask before it
 * spends, where the archive line sits, and how many matches earn the strong
 * model's full read. The settings live on the user, so they can always be
 * saved, resume or not.
 */

type AutonomyMode = Settings["defaultMode"];

const MODES: Array<{ value: AutonomyMode; label: string; detail: string }> = [
  {
    value: "approve_each",
    label: "Ask before spending",
    detail: "Pauses before the deep review and before shortlisting, with a cost estimate.",
  },
  {
    value: "autopilot",
    label: "Autopilot",
    detail: "Scores, reviews and shortlists without stopping.",
  },
];

export function BehaviourForm({
  initial,
  reviewCost,
  reviewModel,
}: {
  initial: Settings;
  /** Rough cost of one deep review, or null when the model is unpriced. */
  reviewCost: number | null;
  reviewModel: string | null;
}) {
  const router = useRouter();
  // The sliders cover the useful range; a stored value outside it (set from
  // chat, say) is shown at the nearest end rather than breaking the slider.
  const baseline = {
    mode: initial.defaultMode,
    threshold: clamp(initial.scoreThreshold, 30, 80),
    topN: clamp(initial.deepEvalTopN, 0, 40),
  };
  const [mode, setMode] = useState<AutonomyMode>(baseline.mode);
  const [threshold, setThreshold] = useState(baseline.threshold);
  const [topN, setTopN] = useState(baseline.topN);
  const [pending, startTransition] = useTransition();

  const dirty =
    mode !== baseline.mode || threshold !== baseline.threshold || topN !== baseline.topN;

  function save() {
    if (!dirty || pending) return;
    startTransition(async () => {
      const result = await call(
        api.PATCH("/api/settings", {
          body: { defaultMode: mode, scoreThreshold: threshold, deepEvalTopN: topN },
        }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      // The baseline is the page's data: once it re-renders with what was saved, the form is
      // clean again and the save bar goes away.
      router.refresh();
    });
  }

  function discard() {
    setMode(baseline.mode);
    setThreshold(baseline.threshold);
    setTopN(baseline.topN);
  }

  useUnsavedChanges(dirty, save);

  const perSearch = reviewCost != null ? reviewCost * topN : null;

  return (
    <div className="space-y-4">
      <fieldset className="min-w-0 divide-y rounded-xl border">
        <div className="space-y-3 px-5 py-4">
          <div className="space-y-1">
            <p className="text-[13.5px] font-medium" id="autonomy-label">
              When a search is about to spend
            </p>
            <p className="text-muted-foreground text-[12.5px]">
              The default for new searches. You can still choose for a single search in chat.
            </p>
          </div>
          <RadioGroup
            aria-labelledby="autonomy-label"
            className="grid gap-2.5 sm:grid-cols-2"
            onValueChange={(value) => setMode(value as AutonomyMode)}
            value={mode}
          >
            {MODES.map((option) => (
              <Radio.Root
                className={cn(
                  "group/mode flex cursor-default gap-3 rounded-lg border px-3.5 py-3 text-left transition-colors outline-none",
                  "hover:bg-muted/50 focus-visible:ring-ring/50 focus-visible:ring-3",
                  "data-checked:border-primary/60 data-checked:bg-accent/60",
                  "data-disabled:opacity-60",
                )}
                key={option.value}
                value={option.value}
              >
                <span
                  aria-hidden
                  className="border-input group-data-checked/mode:border-primary mt-0.5 inline-flex size-4 shrink-0 items-center justify-center rounded-full border"
                >
                  <span className="bg-primary hidden size-2 rounded-full group-data-checked/mode:block" />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13.5px] font-medium">{option.label}</span>
                  <span className="text-muted-foreground mt-0.5 block text-[12.5px] leading-snug">
                    {option.detail}
                  </span>
                </span>
              </Radio.Root>
            ))}
          </RadioGroup>
        </div>

        <SliderRow
          hint="Fast scores under this line are archived with a note instead of landing on your board. Raise it for a shorter board, lower it to see long shots."
          id="threshold"
          label={
            <>
              Archive matches scoring below{" "}
              <span className="text-display tabular text-[15px]">{threshold}</span>
            </>
          }
          max={80}
          min={30}
          onChange={setThreshold}
          value={threshold}
        />

        <SliderRow
          hint={
            topN === 0
              ? "Searches stop after scoring. You can still ask for a deep review of any match."
              : reviewCost != null
                ? `The stronger model reads each posting in full against your resume. About ${formatUsd(reviewCost)} a match${reviewModel ? ` on ${reviewModel}` : ""}, so up to about ${formatUsd(perSearch ?? 0)} a search.`
                : "The stronger model reads each posting in full against your resume, quoting the evidence for every requirement."
          }
          id="top-n"
          label={
            topN === 0 ? (
              "Never run the deep review"
            ) : (
              <>
                Deep review the best{" "}
                <span className="text-display tabular text-[15px]">{topN}</span>{" "}
                {topN === 1 ? "match" : "matches"}
              </>
            )
          }
          max={40}
          min={0}
          onChange={setTopN}
          value={topN}
        />
      </fieldset>

      <SaveBar
        message="Unsaved changes to how Foray works."
        onDiscard={discard}
        onSave={save}
        pending={pending}
        saveLabel="Save settings"
        visible={dirty}
      />
    </div>
  );
}

function SliderRow({
  id,
  label,
  hint,
  value,
  min,
  max,
  onChange,
  disabled,
}: {
  id: string;
  label: React.ReactNode;
  hint: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid gap-x-8 gap-y-3 px-5 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)] sm:items-center">
      <div className="min-w-0 space-y-1">
        <p className="text-[13.5px] font-medium" id={`${id}-label`}>
          {label}
        </p>
        <p className="text-muted-foreground text-[12.5px] leading-snug">{hint}</p>
      </div>
      <div className="space-y-1.5">
        <Slider
          aria-labelledby={`${id}-label`}
          disabled={disabled}
          max={max}
          min={min}
          onValueChange={(next) => onChange(Array.isArray(next) ? (next[0] ?? min) : (next as number))}
          step={1}
          value={[value]}
        />
        <div className="text-muted-foreground tabular flex justify-between text-[11.5px]">
          <span>{min}</span>
          <span>{max}</span>
        </div>
      </div>
    </div>
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
