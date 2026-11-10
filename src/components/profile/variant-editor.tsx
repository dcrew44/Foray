"use client";

import { ArrowDown, ArrowUp, RotateCcw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, useTransition, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { cn } from "@/lib/utils";
import {
  canMove,
  moveRow,
  rowsFor,
  splitKeywords,
  toDraft,
  toSpec,
  type VariantDraft,
  type VariantRow,
} from "@/lib/variants";

import { BulletList } from "./bullet-list";
import { FormRow, SaveBar, TagInput, sameContent, useUnsavedChanges } from "./form-controls";
import type { EditableResume } from "./resume";

/**
 * The editor for one resume variant (#61).
 *
 * A variant chooses what to show from the resume and how to word it; it never holds a fact. So
 * employers, titles, dates and schools are shown here as they stand in the resume, read-only,
 * and what can be edited is the selection, the order and the wording: the headline, the summary
 * and each role's bullets. A role whose bullets were never edited keeps the resume's bullets,
 * and follows them when the resume changes.
 *
 * The draft is laid over the resume on every render, so a role added to the resume while this
 * is open shows up (unchosen) without losing the edits in progress.
 */

type ProfileVariant = components["schemas"]["ProfileVariant"];
type Section = "work" | "projects";

type Draft = VariantDraft & { name: string; keywords: string[]; isDefault: boolean };

function draftOf(base: EditableResume, variant: ProfileVariant): Draft {
  return {
    ...toDraft(base, variant.spec),
    name: variant.name,
    keywords: variant.keywords,
    isDefault: variant.isDefault,
  };
}

/** What a draft would save, for comparing two drafts by what they mean. */
function saved(base: EditableResume, draft: Draft) {
  return {
    name: draft.name.trim(),
    keywords: draft.keywords,
    isDefault: draft.isDefault,
    spec: toSpec(base, draft),
  };
}

export function VariantEditor({
  base,
  variant,
  onDirtyChange,
}: {
  base: EditableResume;
  variant: ProfileVariant;
  /** The list above keeps one editor open at a time, and asks before leaving unsaved edits. */
  onDirtyChange: (dirty: boolean) => void;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const initial = useMemo(() => draftOf(base, variant), [base, variant]);
  const [draft, setDraft] = useState<Draft>(initial);

  const dirty = !sameContent(saved(base, draft), saved(base, initial));
  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);

  const rows: Record<Section, VariantRow[]> = {
    work: rowsFor(base.work, draft.work),
    projects: rowsFor(base.projects, draft.projects),
  };

  function setRows(section: Section, change: (current: VariantRow[]) => VariantRow[]) {
    setDraft((current) => ({
      ...current,
      [section]: change(rowsFor(base[section], current[section])),
    }));
  }

  function patchRow(section: Section, id: string, change: Partial<VariantRow>) {
    setRows(section, (current) =>
      current.map((row) => (row.id === id ? { ...row, ...change } : row)),
    );
  }

  function toggle(list: "education" | "skills", id: string, on: boolean) {
    setDraft((current) => ({
      ...current,
      [list]: on ? [...current[list], id] : current[list].filter((item) => item !== id),
    }));
  }

  function save() {
    if (!dirty || pending) return;
    const body = saved(base, draft);
    startTransition(async () => {
      const result = await call(
        api.PATCH("/api/variants/{id}", { params: { path: { id: variant.id } }, body }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      // The page re-renders with what was stored; the editor is keyed on it and starts over.
      router.refresh();
    });
  }

  useUnsavedChanges(dirty, save);

  const idPrefix = `variant-${variant.id}`;

  return (
    <div className="space-y-8">
      <fieldset className="min-w-0 divide-y rounded-xl border" disabled={pending}>
        <FormRow id={`${idPrefix}-name`} label="Name">
          <Input
            className="h-9 text-[13.5px] md:text-[13.5px]"
            id={`${idPrefix}-name`}
            maxLength={60}
            onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
            value={draft.name}
          />
        </FormRow>
        <FormRow
          hint="Words in a posting's title that choose this variant. Press Enter after each."
          id={`${idPrefix}-keywords`}
          label="Title keywords"
        >
          <TagInput
            id={`${idPrefix}-keywords`}
            onChange={(next) =>
              setDraft((current) => ({ ...current, keywords: splitKeywords(next) }))
            }
            placeholder="backend, api, platform"
            value={draft.keywords}
          />
        </FormRow>
        <div className="grid gap-x-8 gap-y-3 px-5 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <div className="min-w-0 space-y-1">
            <p className="text-[13.5px] font-medium" id={`${idPrefix}-default-label`}>
              Default variant
            </p>
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              Sent when a posting&apos;s title says none of your variants&apos; keywords. Without
              a default, such a posting gets your full resume.
            </p>
          </div>
          <Switch
            aria-labelledby={`${idPrefix}-default-label`}
            checked={draft.isDefault}
            onCheckedChange={(isDefault) => setDraft((current) => ({ ...current, isDefault }))}
          />
        </div>
        <FormRow
          hint="Leave empty to use your resume's headline."
          id={`${idPrefix}-headline`}
          label="Headline"
        >
          <Input
            className="h-9 text-[13.5px] md:text-[13.5px]"
            id={`${idPrefix}-headline`}
            onChange={(event) =>
              setDraft((current) => ({ ...current, headline: event.target.value }))
            }
            placeholder={base.basics.headline ?? undefined}
            value={draft.headline ?? ""}
          />
        </FormRow>
        <FormRow
          hint="Leave empty to use your resume's summary."
          id={`${idPrefix}-summary`}
          label="Summary"
        >
          <Textarea
            className="min-h-24 text-[13.5px]"
            id={`${idPrefix}-summary`}
            onChange={(event) =>
              setDraft((current) => ({ ...current, summary: event.target.value }))
            }
            placeholder={base.summary ?? undefined}
            value={draft.summary ?? ""}
          />
        </FormRow>
      </fieldset>

      <Group
        count={rows.work.filter((row) => row.included).length}
        empty="Your resume has no roles yet."
        of={base.work.length}
        title="Experience"
      >
        {rows.work.map((row) => {
          const item = base.work.find((role) => role.id === row.id);
          if (!item) return null;
          const label = [item.position, item.company].filter(Boolean).join(" at ") || "this role";
          return (
            <ItemRow
              disabled={pending}
              facts={dates(item.startDate, item.endDate)}
              key={row.id}
              label={label}
              onBullets={(highlights) => patchRow("work", row.id, { highlights })}
              onInclude={(included) => patchRow("work", row.id, { included })}
              onMove={(direction) =>
                setRows("work", (current) => moveRow(current, row.id, direction))
              }
              resumeBullets={item.highlights}
              row={row}
              rows={rows.work}
              subtitle={item.company}
              title={item.position}
            />
          );
        })}
      </Group>

      {base.projects.length > 0 && (
        <Group
          count={rows.projects.filter((row) => row.included).length}
          of={base.projects.length}
          title="Projects"
        >
          {rows.projects.map((row) => {
            const item = base.projects.find((project) => project.id === row.id);
            if (!item) return null;
            return (
              <ItemRow
                disabled={pending}
                facts={dates(item.startDate, item.endDate)}
                key={row.id}
                label={item.name || "this project"}
                onBullets={(highlights) => patchRow("projects", row.id, { highlights })}
                onInclude={(included) => patchRow("projects", row.id, { included })}
                onMove={(direction) =>
                  setRows("projects", (current) => moveRow(current, row.id, direction))
                }
                resumeBullets={item.highlights}
                row={row}
                rows={rows.projects}
                subtitle={null}
                title={item.name}
              />
            );
          })}
        </Group>
      )}

      {base.education.length > 0 && (
        <Group
          count={base.education.filter((item) => draft.education.includes(item.id)).length}
          of={base.education.length}
          title="Education"
        >
          {base.education.map((item) => {
            const degree = [item.studyType, item.area].filter(Boolean).join(", ");
            return (
              <li key={item.id}>
                <label className="hover:bg-muted/50 flex cursor-pointer items-start gap-3 px-5 py-3">
                  <Checkbox
                    checked={draft.education.includes(item.id)}
                    className="mt-0.5"
                    disabled={pending}
                    onCheckedChange={(on) => toggle("education", item.id, Boolean(on))}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13.5px] font-medium">{item.institution}</span>
                    <span className="text-muted-foreground block text-[12.5px]">
                      {[degree, dates(item.startDate, item.endDate)].filter(Boolean).join(", ")}
                    </span>
                  </span>
                </label>
              </li>
            );
          })}
        </Group>
      )}

      {base.skills.length > 0 && (
        <section className="space-y-3">
          <GroupTitle
            count={base.skills.filter((skill) => draft.skills.includes(skill.id)).length}
            of={base.skills.length}
            title="Skills"
          />
          <div className="flex flex-wrap gap-1.5 rounded-xl border px-5 py-4">
            {base.skills.map((skill) => {
              const on = draft.skills.includes(skill.id);
              return (
                <button
                  aria-pressed={on}
                  className={cn(
                    "focus-visible:ring-ring/50 inline-flex h-7 items-center rounded-md px-2.5 text-[13px] transition-colors outline-none focus-visible:ring-3 disabled:opacity-50",
                    on
                      ? "bg-secondary text-secondary-foreground"
                      : "text-muted-foreground hover:text-foreground border border-dashed",
                  )}
                  disabled={pending}
                  key={skill.id}
                  onClick={() => toggle("skills", skill.id, !on)}
                  title={skill.category ?? undefined}
                  type="button"
                >
                  {skill.name}
                </button>
              );
            })}
          </div>
        </section>
      )}

      <SaveBar
        message={`Unsaved changes to ${draft.name.trim() || "this variant"}.`}
        onDiscard={() => setDraft(initial)}
        onSave={save}
        pending={pending}
        visible={dirty}
      />
    </div>
  );
}

/** "June 2020 to February 2023", as the resume wrote its dates. */
function dates(start: string | null | undefined, end: string | null | undefined): string {
  return [start, end].filter(Boolean).join(" to ");
}

function GroupTitle({ title, count, of }: { title: string; count: number; of: number }) {
  return (
    <h4 className="text-[14px] font-semibold">
      {title}
      <span className="text-muted-foreground tabular ml-2 text-[13px] font-normal">
        {count} of {of}
      </span>
    </h4>
  );
}

function Group({
  title,
  count,
  of,
  empty,
  children,
}: {
  title: string;
  count: number;
  of: number;
  empty?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <GroupTitle count={count} of={of} title={title} />
      {of === 0 && empty ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
          {empty}
        </p>
      ) : (
        <ol className="divide-y rounded-xl border">{children}</ol>
      )}
    </section>
  );
}

/**
 * One role or project: whether the variant shows it, where, and in what words. The facts on
 * the left are the resume's and cannot be edited here.
 */
function ItemRow({
  row,
  rows,
  title,
  subtitle,
  facts,
  label,
  resumeBullets,
  disabled,
  onInclude,
  onMove,
  onBullets,
}: {
  row: VariantRow;
  rows: VariantRow[];
  title: string;
  subtitle: string | null;
  facts: string;
  /** How the row is named to a screen reader: "Software Engineer at Harbor Analytics". */
  label: string;
  resumeBullets: string[];
  disabled: boolean;
  onInclude: (included: boolean) => void;
  onMove: (direction: -1 | 1) => void;
  onBullets: (highlights: string[] | null) => void;
}) {
  const reworded =
    row.highlights !== null &&
    !sameContent(
      row.highlights.map((line) => line.trim()).filter(Boolean),
      resumeBullets,
    );

  return (
    <li className={cn("space-y-3 px-5 py-4", !row.included && "bg-muted/30")}>
      <div className="flex items-start gap-3">
        <Checkbox
          aria-label={`Include ${label}`}
          checked={row.included}
          className="mt-0.5"
          disabled={disabled}
          onCheckedChange={(on) => onInclude(Boolean(on))}
        />
        <div className={cn("min-w-0 flex-1", !row.included && "text-muted-foreground")}>
          <p className="text-[13.5px] font-medium">{title || label}</p>
          {(subtitle || facts) && (
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              {[subtitle, facts].filter(Boolean).join(", ")}
            </p>
          )}
        </div>
        {row.included && (
          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              aria-label={`Move ${label} up`}
              className="text-muted-foreground"
              disabled={disabled || !canMove(rows, row.id, -1)}
              onClick={() => onMove(-1)}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              <ArrowUp />
            </Button>
            <Button
              aria-label={`Move ${label} down`}
              className="text-muted-foreground"
              disabled={disabled || !canMove(rows, row.id, 1)}
              onClick={() => onMove(1)}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              <ArrowDown />
            </Button>
          </div>
        )}
      </div>
      {row.included && (
        <div className="space-y-1.5 sm:pl-7">
          <BulletList
            label={reworded ? "Bullets, in this variant's words" : "Bullets, as your resume has them"}
            onChange={onBullets}
            value={row.highlights ?? resumeBullets}
          />
          {reworded && (
            <Button
              className="text-muted-foreground"
              disabled={disabled}
              onClick={() => onBullets(null)}
              size="sm"
              type="button"
              variant="ghost"
            >
              <RotateCcw />
              Use the resume&apos;s bullets
            </Button>
          )}
        </div>
      )}
    </li>
  );
}
