"use client";

import { Plus, Trash, X } from "lucide-react";
import { useState, type ClipboardEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import type { SkillItem } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/**
 * Skills as chips, grouped the way the resume grouped them.
 *
 * A group is only a category name shared by its skills — the schema has no
 * group object — so renaming a group rewrites the category on each of its
 * skills, and a group that has just been created lives here until its first
 * skill gives it a home in the data.
 */

type Group = { key: string; name: string; skills: SkillItem[] };

function groupSkills(skills: SkillItem[], pending: string[]): Group[] {
  const groups = new Map<string, Group>();
  for (const skill of skills) {
    const name = skill.category?.trim() ?? "";
    const key = name.toLowerCase();
    const group = groups.get(key) ?? { key, name, skills: [] };
    group.skills.push(skill);
    groups.set(key, group);
  }
  for (const name of pending) {
    const key = name.trim().toLowerCase();
    if (!groups.has(key)) groups.set(key, { key, name: name.trim(), skills: [] });
  }
  // Uncategorised skills read best at the end, under "Other".
  const uncategorised = groups.get("");
  groups.delete("");
  return uncategorised ? [...groups.values(), uncategorised] : [...groups.values()];
}

export function SkillsEditor({
  skills,
  onChange,
}: {
  skills: SkillItem[];
  onChange: (next: SkillItem[]) => void;
}) {
  const [pending, setPending] = useState<string[]>([]);
  // A group made a moment ago gets its name field focused; once named, focus
  // moves on to adding its first skill.
  const [naming, setNaming] = useState<string | null>(null);
  const [filling, setFilling] = useState<string | null>(null);
  const groups = groupSkills(skills, pending);

  function addSkills(group: Group, names: string[]) {
    const existing = new Set(group.skills.map((skill) => skill.name.toLowerCase()));
    const fresh = names
      .map((name) => name.trim())
      .filter((name) => name && !existing.has(name.toLowerCase()))
      .filter((name, index, all) => all.findIndex((n) => n.toLowerCase() === name.toLowerCase()) === index);
    if (fresh.length === 0) return;

    onChange([
      ...skills,
      ...fresh.map((name) => ({
        id: crypto.randomUUID(),
        source: "user" as const,
        name,
        category: group.name || null,
      })),
    ]);
    setPending((names) => names.filter((n) => n.trim().toLowerCase() !== group.key));
  }

  function renameGroup(group: Group, nextName: string) {
    const name = nextName.trim();
    if (naming === group.key) {
      setNaming(null);
      setFilling(name.toLowerCase());
    }
    if (name === group.name) return;
    if (group.skills.length === 0) {
      setPending((names) => names.map((n) => (n.trim().toLowerCase() === group.key ? name : n)));
      return;
    }
    const ids = new Set(group.skills.map((skill) => skill.id));
    onChange(
      skills.map((skill) => (ids.has(skill.id) ? { ...skill, category: name || null } : skill)),
    );
  }

  function removeGroup(group: Group) {
    if (group.skills.length === 0) {
      setPending((names) => names.filter((n) => n.trim().toLowerCase() !== group.key));
      return;
    }
    const ids = new Set(group.skills.map((skill) => skill.id));
    onChange(skills.filter((skill) => !ids.has(skill.id)));
  }

  function newGroup() {
    let name = "New group";
    let n = 2;
    const taken = new Set(groups.map((group) => group.key));
    while (taken.has(name.toLowerCase())) name = `New group ${n++}`;
    setPending((names) => [...names, name]);
    setNaming(name.toLowerCase());
  }

  return (
    <div className="space-y-3">
      {groups.length > 0 && (
        <div className="divide-y rounded-xl border">
          {groups.map((group) => (
            <div
              className="grid gap-x-6 gap-y-2.5 px-5 py-4 sm:grid-cols-[10rem_minmax(0,1fr)]"
              key={group.key || "__other"}
            >
              <div className="flex items-start gap-1">
                <GroupName
                  autoFocus={naming === group.key}
                  key={group.name}
                  name={group.name}
                  onCommit={(name) => renameGroup(group, name)}
                />
                <Button
                  aria-label={`Remove the ${group.name || "Other"} group`}
                  className="text-muted-foreground mt-0.5 sm:hidden"
                  onClick={() => removeGroup(group)}
                  size="icon-sm"
                  type="button"
                  variant="ghost"
                >
                  <Trash />
                </Button>
              </div>
              <div className="group/skills flex min-w-0 flex-wrap items-center gap-1.5">
                {group.skills.map((skill) => (
                  <SkillChip
                    key={skill.id}
                    onRemove={() => onChange(skills.filter((item) => item.id !== skill.id))}
                    onRename={(name) =>
                      onChange(
                        name
                          ? skills.map((item) => (item.id === skill.id ? { ...item, name } : item))
                          : skills.filter((item) => item.id !== skill.id),
                      )
                    }
                    skill={skill}
                  />
                ))}
                <AddSkill
                  autoFocus={filling === group.key}
                  groupName={group.name || "Other"}
                  // Remounted to take focus: autoFocus only acts on mount.
                  key={filling === group.key ? "add-focused" : "add"}
                  onAdd={(names) => addSkills(group, names)}
                />
                <Button
                  aria-label={`Remove the ${group.name || "Other"} group`}
                  className="text-muted-foreground ml-auto hidden sm:inline-flex sm:opacity-0 sm:group-focus-within/skills:opacity-100 sm:group-hover/skills:opacity-100 sm:focus-visible:opacity-100"
                  onClick={() => removeGroup(group)}
                  size="icon-sm"
                  title="Remove group"
                  type="button"
                  variant="ghost"
                >
                  <Trash />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      <Button onClick={newGroup} size="sm" type="button" variant="outline">
        <Plus />
        Add a group
      </Button>
    </div>
  );
}

/** The group's name, edited in place. Commits on Enter or when focus leaves. */
function GroupName({
  name,
  onCommit,
  autoFocus,
}: {
  name: string;
  onCommit: (name: string) => void;
  autoFocus?: boolean;
}) {
  const [draft, setDraft] = useState(name);

  return (
    <input
      aria-label="Group name"
      autoFocus={autoFocus}
      onFocus={(event) => {
        if (autoFocus) event.currentTarget.select();
      }}
      className={cn(
        "hover:bg-muted focus:bg-background focus:border-ring focus:ring-ring/50 -mx-2 h-8 w-full min-w-0 rounded-md border border-transparent bg-transparent px-2 text-[13.5px] font-medium outline-none focus:ring-3",
        "placeholder:text-muted-foreground placeholder:font-normal",
      )}
      onBlur={() => onCommit(draft)}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") {
          setDraft(name);
          event.currentTarget.blur();
        }
      }}
      placeholder="Other"
      value={draft}
    />
  );
}

/** A skill chip: click the name to edit it, the cross to remove it. */
function SkillChip({
  skill,
  onRename,
  onRemove,
}: {
  skill: SkillItem;
  onRename: (name: string) => void;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(skill.name);

  if (editing) {
    return (
      <input
        aria-label={`Edit ${skill.name}`}
        autoFocus
        className="border-ring ring-ring/50 field-sizing-content h-7 min-w-[6ch] rounded-md border bg-transparent px-2 text-[13px] outline-none ring-3"
        onBlur={() => {
          setEditing(false);
          if (draft.trim() !== skill.name) onRename(draft.trim());
        }}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") {
            setDraft(skill.name);
            setEditing(false);
          }
        }}
        value={draft}
      />
    );
  }

  return (
    <span
      className="bg-secondary text-secondary-foreground inline-flex h-7 max-w-full items-center rounded-md text-[13px]"
      title={
        skill.source === "user"
          ? "Added or edited by you"
          : skill.source === "linkedin_pdf"
            ? "From LinkedIn"
            : "From your resume"
      }
    >
      <button
        className="focus-visible:ring-ring/60 h-full truncate rounded-l-md pr-1 pl-2.5 outline-none focus-visible:ring-2"
        onClick={() => {
          setDraft(skill.name);
          setEditing(true);
        }}
        type="button"
      >
        <span className="sr-only">Edit </span>
        {skill.name}
      </button>
      <button
        aria-label={`Remove ${skill.name}`}
        className="text-muted-foreground hover:text-foreground hover:bg-foreground/10 focus-visible:ring-ring/60 mr-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded outline-none focus-visible:ring-2"
        onClick={onRemove}
        type="button"
      >
        <X className="size-3" />
      </button>
    </span>
  );
}

function AddSkill({
  groupName,
  onAdd,
  autoFocus,
}: {
  groupName: string;
  onAdd: (names: string[]) => void;
  autoFocus?: boolean;
}) {
  const [draft, setDraft] = useState("");

  function commit(text: string) {
    const names = text.split(/\r?\n/).map((name) => name.trim()).filter(Boolean);
    if (names.length) onAdd(names);
    setDraft("");
  }

  return (
    <input
      aria-label={`Add a skill to ${groupName}`}
      autoFocus={autoFocus}
      className="placeholder:text-muted-foreground focus:border-ring focus:ring-ring/50 h-7 w-28 rounded-md border border-dashed bg-transparent px-2 text-[13px] outline-none focus:w-40 focus:border-solid focus:ring-3"
      onBlur={() => commit(draft)}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commit(draft);
        }
      }}
      onPaste={(event: ClipboardEvent<HTMLInputElement>) => {
        const text = event.clipboardData.getData("text");
        if (!/\r?\n/.test(text)) return;
        event.preventDefault();
        commit(text);
      }}
      placeholder="Add a skill"
      value={draft}
    />
  );
}
