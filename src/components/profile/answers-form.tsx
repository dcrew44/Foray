"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

import { FormRow, SaveBar, Segmented, useUnsavedChanges } from "./form-controls";

/**
 * The questions application forms keep asking, answered once (#21). Foray
 * fills a form only from these: it never writes an answer about sponsorship,
 * pay or experience itself, and a voluntary disclosure left empty here is
 * answered "Decline to answer".
 */

type SavedAnswer = components["schemas"]["SavedAnswer"];
type Group = SavedAnswer["group"];

const GROUPS: Array<{ id: Group; title: string; note?: string }> = [
  {
    id: "work",
    title: "Work authorization",
    note: "These also keep roles you could not be hired for out of your searches.",
  },
  { id: "logistics", title: "The usual questions" },
  { id: "links", title: "Links" },
  {
    id: "disclosures",
    title: "Voluntary disclosures",
    note: "Leave any of these empty and Foray answers “Decline to answer”.",
  },
  {
    id: "custom",
    title: "Your own answers",
    note: "Questions you answered on an application. Empty one to forget it.",
  },
];

/** A row's key in the draft: the kind, and for a custom answer its question too. */
function keyOf(item: SavedAnswer): string {
  return item.kind === "custom" ? `custom:${item.question}` : item.kind;
}

export function AnswersForm({ initial }: { initial: SavedAnswer[] }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const stored = useMemo(
    () => Object.fromEntries(initial.map((item) => [keyOf(item), item.answer ?? ""])),
    [initial],
  );
  const [draft, setDraft] = useState<Record<string, string>>(stored);

  const changed = initial.filter((item) => (draft[keyOf(item)] ?? "").trim() !== stored[keyOf(item)]);
  const dirty = changed.length > 0;

  function save() {
    if (!dirty || pending) return;
    startTransition(async () => {
      const result = await call(
        api.PUT("/api/answers", {
          body: {
            answers: changed.map((item) => ({
              kind: item.kind,
              question: item.question,
              answer: (draft[keyOf(item)] ?? "").trim(),
            })),
          },
        }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      // The page re-renders with what was stored; the form is keyed on it and starts over.
      router.refresh();
    });
  }

  useUnsavedChanges(dirty, save);

  return (
    <div className="space-y-8">
      {GROUPS.map((group) => {
        const items = initial.filter((item) => item.group === group.id);
        if (items.length === 0) return null;
        return (
          <div className="space-y-2.5" key={group.id}>
            <div className="px-1">
              <h3 className="text-[14px] font-semibold">{group.title}</h3>
              {group.note && (
                <p className="text-muted-foreground mt-0.5 text-[12.5px] leading-snug">
                  {group.note}
                </p>
              )}
            </div>
            <fieldset className="min-w-0 divide-y rounded-xl border" disabled={pending}>
              {items.map((item, index) => {
                const key = keyOf(item);
                const id = `answer-${group.id}-${index}`;
                const value = draft[key] ?? "";
                const set = (next: string) => setDraft((current) => ({ ...current, [key]: next }));
                return (
                  <FormRow hint={item.hint || undefined} id={id} key={key} label={item.question}>
                    {item.input === "text" ? (
                      item.kind === "custom" ? (
                        <Textarea
                          className="min-h-20 text-[13.5px]"
                          id={id}
                          onChange={(event) => set(event.target.value)}
                          value={value}
                        />
                      ) : (
                        <Input
                          className="h-9 text-[13.5px] md:text-[13.5px]"
                          id={id}
                          onChange={(event) => set(event.target.value)}
                          value={value}
                        />
                      )
                    ) : (
                      <Segmented
                        labelledBy={`${id}-label`}
                        onChange={set}
                        options={[
                          ...item.options.map((option) => ({
                            value: option.value,
                            label: option.label,
                          })),
                          { value: "", label: "Not said" },
                        ]}
                        value={value}
                      />
                    )}
                  </FormRow>
                );
              })}
            </fieldset>
          </div>
        );
      })}
      <SaveBar
        onDiscard={() => setDraft(stored)}
        onSave={save}
        pending={pending}
        visible={dirty}
      />
    </div>
  );
}
