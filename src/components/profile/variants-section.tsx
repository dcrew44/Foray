"use client";

import { Download, FileText, Pencil, Plus, Trash } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/foray/primitives";
import { isActiveRun, requestLiveRefresh, useRun } from "@/components/live/live-provider";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { plural } from "@/lib/format";
import { selectionCounts, selectsNothing, splitKeywords } from "@/lib/variants";

import { FormRow, TagInput } from "./form-controls";
import type { EditableResume } from "./resume";
import { VariantEditor } from "./variant-editor";

/**
 * Resume variants (#61): two to four versions of the resume, one per kind of role. Foray picks
 * one for each application by the posting's title, and the user can change the pick there.
 *
 * A variant is made from the resume above and edited here; it is not uploaded, and it holds no
 * facts of its own. Each has its own PDF, made in the worker like the resume's: the page
 * refreshes on the run's `run.completed`, which brings the new PDF's link.
 */

type ProfileVariant = components["schemas"]["ProfileVariant"];

export function VariantsSection({
  base,
  variants,
  limit,
}: {
  /** The newest base resume: every fact a variant shows comes from it. */
  base: EditableResume;
  variants: ProfileVariant[];
  /** How many variants a user may keep: Django's number, so there is no copy here. */
  limit: number;
}) {
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  // Whether the open editor holds unsaved edits: asked before it is closed or swapped.
  const dirty = useRef(false);
  const onDirtyChange = useCallback((next: boolean) => {
    dirty.current = next;
  }, []);

  const open = variants.find((variant) => variant.id === openId) ?? null;

  /** Opens or closes an editor, unless the one open would lose edits. */
  function show(id: string | null) {
    if (dirty.current && open && id !== open.id) {
      toast(`Save or discard your changes to ${open.name} first.`);
      return;
    }
    setOpenId(id);
  }

  if (variants.length === 0 && !adding) {
    return (
      <EmptyState
        action={
          <Button onClick={() => setAdding(true)} variant="outline">
            <Plus />
            New variant
          </Button>
        }
        title="One resume per kind of role"
      >
        Keep two to four variants, say one for backend roles and one for data roles. Each
        chooses what to show from your resume and how to word it, and Foray picks one for every
        application by the posting&apos;s title. Employers, titles, dates and schools always
        come from the resume above, so a variant can never contradict it.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-3">
      {variants.length > 0 && (
        <ul className="divide-y rounded-xl border">
          {variants.map((variant) => (
            <VariantListItem
              base={base}
              editing={variant.id === openId}
              key={variant.id}
              onDirtyChange={onDirtyChange}
              onEdit={() => show(variant.id === openId ? null : variant.id)}
              onDeleted={() => {
                if (variant.id === openId) setOpenId(null);
              }}
              variant={variant}
            />
          ))}
        </ul>
      )}

      {adding ? (
        <NewVariant
          onCancel={() => setAdding(false)}
          onCreated={(id) => {
            setAdding(false);
            // The new variant shows everything; its editor opens so it can be narrowed.
            if (!dirty.current) setOpenId(id);
          }}
        />
      ) : variants.length < limit ? (
        <Button onClick={() => setAdding(true)} size="sm" variant="outline">
          <Plus />
          New variant
        </Button>
      ) : (
        <p className="text-muted-foreground px-1 text-[12.5px]">
          You can keep up to {limit} variants. Delete one to add another.
        </p>
      )}
    </div>
  );
}

/** "2 roles, 1 project and 4 skills" */
function joinList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

function VariantListItem({
  base,
  variant,
  editing,
  onEdit,
  onDeleted,
  onDirtyChange,
}: {
  base: EditableResume;
  variant: ProfileVariant;
  editing: boolean;
  onEdit: () => void;
  onDeleted: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const router = useRouter();
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, startRender] = useTransition();
  const [confirming, setConfirming] = useState(false);
  const [deleting, startDelete] = useTransition();
  const run = useRun(runId);
  const rendering = starting || isActiveRun(run);

  const counts = selectionCounts(base, variant.spec);
  const empty = selectsNothing(counts);
  const shows = joinList(
    [
      counts.work > 0 ? plural(counts.work, "role") : null,
      counts.projects > 0 ? plural(counts.projects, "project") : null,
      counts.education > 0 ? plural(counts.education, "school") : null,
      counts.skills > 0 ? plural(counts.skills, "skill") : null,
    ].filter((part): part is string => part !== null),
  );

  function render() {
    startRender(async () => {
      const result = await call(
        api.POST("/api/variants/{id}/render", { params: { path: { id: variant.id } } }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setRunId(result.runId);
      requestLiveRefresh();
    });
  }

  function remove() {
    startDelete(async () => {
      const result = await call(
        api.DELETE("/api/variants/{id}", { params: { path: { id: variant.id } } }),
      );
      if (!result.ok) {
        toast.error(result.message);
        setConfirming(false);
        return;
      }
      toast.success(result.message);
      onDeleted();
      router.refresh();
    });
  }

  return (
    <li>
      <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1.5">
          <p className="flex flex-wrap items-center gap-2 text-[14px] font-semibold">
            {variant.name}
            {variant.isDefault && <Badge variant="secondary">Default</Badge>}
          </p>
          {variant.keywords.length > 0 ? (
            <ul aria-label="Title keywords" className="flex flex-wrap gap-1.5">
              {variant.keywords.map((keyword) => (
                <li
                  className="bg-secondary text-secondary-foreground inline-flex h-6 items-center rounded-md px-2 text-[12.5px]"
                  key={keyword}
                >
                  {keyword}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground text-[12.5px]">
              {variant.isDefault
                ? "No title keywords: sent only when no other variant matches."
                : "No title keywords yet, so no posting chooses it."}
            </p>
          )}
          {empty ? (
            <p className="text-signal-foreground bg-signal-soft rounded-md px-2 py-1 text-[12.5px]">
              It selects nothing from your current resume, so it is never sent. Edit it to choose
              what it shows.
            </p>
          ) : (
            <p className="text-muted-foreground text-[12.5px]">Shows {shows}.</p>
          )}
          {variant.pdf && !variant.pdf.current && !rendering && (
            <p className="text-muted-foreground text-[12.5px]">
              The PDF on file was made before your latest changes.
            </p>
          )}
        </div>

        {confirming ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <p className="text-[13px]">Delete this variant?</p>
            <Button disabled={deleting} onClick={() => setConfirming(false)} size="sm" variant="ghost">
              Cancel
            </Button>
            <Button disabled={deleting} onClick={remove} size="sm" variant="destructive">
              {deleting ? <Spinner /> : <Trash />}
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </div>
        ) : (
          <div className="flex shrink-0 flex-wrap items-center gap-1.5">
            {variant.pdf && (
              <a
                className={buttonVariants({ variant: "outline", size: "sm" })}
                href={`/api/documents/${variant.pdf.documentId}?download=1`}
                title={
                  variant.pdf.current
                    ? "This variant as it stands"
                    : "Made before your latest changes"
                }
              >
                <Download />
                Download PDF
              </a>
            )}
            <Button
              disabled={rendering || empty}
              onClick={render}
              size="sm"
              variant={variant.pdf?.current ? "ghost" : "outline"}
            >
              {rendering ? <Spinner /> : <FileText />}
              {rendering ? "Making the PDF…" : "Make PDF"}
            </Button>
            <Button aria-expanded={editing} onClick={onEdit} size="sm" variant="ghost">
              <Pencil />
              {editing ? "Close" : "Edit"}
            </Button>
            <Button
              aria-label={`Delete ${variant.name}`}
              className="text-muted-foreground"
              onClick={() => setConfirming(true)}
              size="icon-sm"
              title="Delete"
              variant="ghost"
            >
              <Trash />
            </Button>
          </div>
        )}
      </div>

      {editing && (
        <div className="border-t px-5 pt-5 pb-4">
          <VariantEditor
            base={base}
            // Starts over from what was stored after each save.
            key={variant.updatedAt}
            onDirtyChange={onDirtyChange}
            variant={variant}
          />
        </div>
      )}
    </li>
  );
}

function NewVariant({
  onCreated,
  onCancel,
}: {
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [keywords, setKeywords] = useState<string[]>([]);
  const [pending, startTransition] = useTransition();

  function create() {
    if (pending || name.trim() === "") return;
    startTransition(async () => {
      const result = await call(
        api.POST("/api/variants", { body: { name: name.trim(), keywords } }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message, {
        description: "It starts with everything in your resume. Choose what it shows.",
      });
      onCreated(result.id);
      router.refresh();
    });
  }

  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        create();
      }}
    >
      <fieldset className="min-w-0 divide-y rounded-xl border" disabled={pending}>
        <FormRow
          hint="The kind of role it is for. It also ends the PDF's file name."
          id="new-variant-name"
          label="Name"
        >
          <Input
            autoFocus
            className="h-9 text-[13.5px] md:text-[13.5px]"
            id="new-variant-name"
            maxLength={60}
            onChange={(event) => setName(event.target.value)}
            placeholder="Backend"
            value={name}
          />
        </FormRow>
        <FormRow
          hint="Words in a posting's title that choose this variant. Press Enter after each."
          id="new-variant-keywords"
          label="Title keywords"
        >
          <TagInput
            id="new-variant-keywords"
            onChange={(next) => setKeywords(splitKeywords(next))}
            placeholder="backend, api, platform"
            value={keywords}
          />
        </FormRow>
      </fieldset>
      <div className="flex items-center gap-2">
        <Button disabled={pending || name.trim() === ""} type="submit">
          {pending && <Spinner />}
          {pending ? "Creating…" : "Create variant"}
        </Button>
        <Button disabled={pending} onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
      </div>
    </form>
  );
}
