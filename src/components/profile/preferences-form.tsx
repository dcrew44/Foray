"use client";

import { useState, useTransition } from "react";
import { toast } from "sonner";

import { savePreferencesAction } from "@/app/actions/profile";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  SLOTS,
  type Preferences,
  type SlotId,
} from "@/lib/profile/interview";
import { cn } from "@/lib/utils";

import {
  FormRow,
  SaveBar,
  Segmented,
  TagInput,
  sameContent,
  useUnsavedChanges,
} from "./form-controls";

/**
 * What the user is looking for, as a form — the same answers the chat
 * interview collects, for anyone who would rather fill them in than talk.
 */

type Seniority = NonNullable<Preferences["seniority"]>;
type Remote = NonNullable<Preferences["remote_pref"]>;
type Period = NonNullable<Preferences["salary_floor"]>["period"];

const SENIORITY: Array<{ value: Seniority; label: string }> = [
  { value: "intern", label: "Internship" },
  { value: "new_grad", label: "New grad" },
  { value: "entry", label: "Entry level" },
  { value: "mid", label: "Mid level" },
  { value: "senior", label: "Senior" },
  { value: "staff_plus", label: "Staff and above" },
];

const REMOTE: Array<{ value: Remote; label: string }> = [
  { value: "remote", label: "Remote" },
  { value: "hybrid", label: "Hybrid" },
  { value: "onsite", label: "On-site" },
  { value: "any", label: "No preference" },
];

const PERIOD: Array<{ value: Period; label: string }> = [
  { value: "year", label: "A year" },
  { value: "hour", label: "An hour" },
];

type Draft = {
  target_roles: string[];
  seniority: Seniority | null;
  locations: string[];
  remote_pref: Remote | null;
  salary_amount: string;
  salary_period: Period;
  salary_currency: string;
  work_auth: string;
  start_date: string;
  must_haves: string[];
  dealbreakers: string[];
};

function toDraft(preferences: Preferences): Draft {
  const salary = preferences.salary_floor;
  return {
    target_roles: preferences.target_roles ?? [],
    seniority: preferences.seniority,
    locations: preferences.locations ?? [],
    remote_pref: preferences.remote_pref,
    salary_amount: salary ? formatAmount(salary.amount, salary.period) : "",
    salary_period: salary?.period ?? "year",
    salary_currency: salary?.currency ?? "USD",
    work_auth: preferences.work_auth ?? "",
    start_date: preferences.start_date ?? "",
    must_haves: preferences.must_haves ?? [],
    dealbreakers: preferences.dealbreakers ?? [],
  };
}

/** Empty answers are stored as null: "not known", the same as never asked. */
function toPreferences(draft: Draft): Preferences {
  const list = (items: string[]) => {
    const cleaned = items.map((item) => item.trim()).filter(Boolean);
    return cleaned.length ? cleaned : null;
  };
  const text = (value: string) => value.trim() || null;
  const amount = parseAmount(draft.salary_amount);

  return {
    target_roles: list(draft.target_roles),
    seniority: draft.seniority,
    locations: list(draft.locations),
    remote_pref: draft.remote_pref,
    salary_floor:
      amount != null && amount > 0
        ? { amount, currency: draft.salary_currency, period: draft.salary_period }
        : null,
    work_auth: text(draft.work_auth),
    start_date: text(draft.start_date),
    must_haves: list(draft.must_haves),
    dealbreakers: list(draft.dealbreakers),
  };
}

function parseAmount(value: string): number | null {
  const cleaned = value.replace(/[^0-9.]/g, "");
  if (!cleaned) return null;
  const amount = Number(cleaned);
  return Number.isFinite(amount) ? amount : null;
}

function formatAmount(amount: number, period: Period): string {
  return amount.toLocaleString("en-US", {
    maximumFractionDigits: period === "hour" ? 2 : 0,
  });
}

const REQUIRED = new Set<SlotId>(SLOTS.filter((slot) => slot.required).map((slot) => slot.id));

export function PreferencesForm({
  initial,
  canSave,
}: {
  initial: Preferences;
  /** False until a resume upload has created the profile these live on. */
  canSave: boolean;
}) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(initial));
  const [pending, startTransition] = useTransition();

  const baseline = toPreferences(toDraft(initial));
  const next = toPreferences(draft);
  const dirty = !sameContent(next, baseline);

  function set(change: Partial<Draft>) {
    setDraft((current) => ({ ...current, ...change }));
  }

  function save() {
    if (!dirty || pending) return;
    startTransition(async () => {
      const result = await savePreferencesAction(next);
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
    });
  }

  useUnsavedChanges(dirty, save);

  const needed = (slot: SlotId) =>
    REQUIRED.has(slot) ? (
      <p className="text-muted-foreground text-[12px]">Needed to search</p>
    ) : null;

  const currencySymbol = draft.salary_currency === "USD" ? "$" : draft.salary_currency;

  return (
    <div className="space-y-4">
      <fieldset className="min-w-0 divide-y rounded-xl border" disabled={!canSave}>
        <FormRow
          hint="Job titles, or a description of the work. Press Enter after each."
          id="pref-roles"
          label="Target roles"
          note={needed("target_roles")}
        >
          <TagInput
            id="pref-roles"
            onChange={(target_roles) => set({ target_roles })}
            placeholder="Senior backend engineer"
            value={draft.target_roles}
          />
        </FormRow>

        <FormRow id="pref-seniority" label="Seniority" note={needed("seniority")}>
          <Select
            items={SENIORITY}
            onValueChange={(value) => set({ seniority: (value as Seniority | null) ?? null })}
            value={draft.seniority}
          >
            <SelectTrigger
              aria-labelledby="pref-seniority-label"
              className="h-9 w-full text-[13.5px] sm:w-60"
              id="pref-seniority"
            >
              <SelectValue placeholder="Choose a level" />
            </SelectTrigger>
            <SelectContent>
              {SENIORITY.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </FormRow>

        <FormRow
          hint="Cities, metros or regions. “Anywhere in the US” works too."
          id="pref-locations"
          label="Locations"
          note={needed("locations")}
        >
          <TagInput
            id="pref-locations"
            onChange={(locations) => set({ locations })}
            placeholder="San Francisco Bay Area"
            value={draft.locations}
          />
        </FormRow>

        <FormRow id="pref-remote" label="Remote preference" note={needed("remote_pref")}>
          <Segmented
            // Two by two on a phone, where four in a row would wrap unevenly.
            className="grid w-full grid-cols-2 sm:inline-flex sm:w-auto"
            labelledBy="pref-remote-label"
            onChange={(remote_pref) => set({ remote_pref })}
            options={REMOTE}
            value={draft.remote_pref}
          />
        </FormRow>

        <FormRow
          hint="Whether you can work without visa sponsorship. It rules postings in or out."
          id="pref-auth"
          label="Work authorisation"
          note={needed("work_auth")}
        >
          <Input
            className="h-9 text-[13.5px] md:text-[13.5px]"
            id="pref-auth"
            onChange={(event) => set({ work_auth: event.target.value })}
            placeholder="US citizen, no sponsorship needed"
            value={draft.work_auth}
          />
        </FormRow>

        <FormRow
          hint="The lowest pay worth your time. Roles that state less rank lower."
          id="pref-salary"
          label="Salary floor"
        >
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-40">
              <span
                aria-hidden
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-[13.5px]"
              >
                {currencySymbol}
              </span>
              <Input
                aria-label={`Salary floor in ${draft.salary_currency}`}
                className={cn(
                  "tabular h-9 text-[13.5px] md:text-[13.5px]",
                  currencySymbol.length > 1 ? "pl-11" : "pl-6",
                )}
                id="pref-salary"
                inputMode="decimal"
                onBlur={() => {
                  const amount = parseAmount(draft.salary_amount);
                  set({
                    salary_amount: amount != null ? formatAmount(amount, draft.salary_period) : "",
                  });
                }}
                onChange={(event) => set({ salary_amount: event.target.value })}
                placeholder={draft.salary_period === "hour" ? "60" : "150,000"}
                value={draft.salary_amount}
              />
            </div>
            <Segmented
              ariaLabel="Salary period"
              onChange={(salary_period) => set({ salary_period })}
              options={PERIOD}
              value={draft.salary_period}
            />
          </div>
        </FormRow>

        <FormRow hint="A date, or “immediately”." id="pref-start" label="Start date">
          <Input
            className="h-9 text-[13.5px] sm:w-60 md:text-[13.5px]"
            id="pref-start"
            onChange={(event) => set({ start_date: event.target.value })}
            placeholder="Immediately"
            value={draft.start_date}
          />
        </FormRow>

        <FormRow hint="Anything a role has to have." id="pref-must" label="Must-haves">
          <TagInput
            id="pref-must"
            onChange={(must_haves) => set({ must_haves })}
            placeholder="Real ownership of a service"
            value={draft.must_haves}
          />
        </FormRow>

        <FormRow
          hint="Anything that rules a role out, like heavy on-call or full return to office."
          id="pref-deal"
          label="Dealbreakers"
        >
          <TagInput
            id="pref-deal"
            onChange={(dealbreakers) => set({ dealbreakers })}
            placeholder="Full return to office"
            value={draft.dealbreakers}
          />
        </FormRow>
      </fieldset>

      <SaveBar
        message="Unsaved changes to your preferences."
        onDiscard={() => setDraft(toDraft(initial))}
        onSave={save}
        pending={pending}
        saveLabel="Save preferences"
        visible={dirty && canSave}
      />
    </div>
  );
}
