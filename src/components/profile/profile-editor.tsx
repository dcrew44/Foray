"use client";

import { Plus, Trash, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, useTransition, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, call } from "@/lib/api/client";

import { BulletList } from "./bullet-list";
import { AreaField, Field, SourceNote } from "./field";
import { SaveBar, sameContent, useUnsavedChanges } from "./form-controls";
import type { EditableResume as Resume } from "./resume";
import { SkillsEditor } from "./skills-editor";

/**
 * The profile editor.
 *
 * Extraction is good but not perfect, and the profile is the factual basis for
 * everything the product later claims on the user's behalf — so correcting it
 * has to be easy and the corrections have to stick. The whole resume is held in
 * local state and saved in one go; the server works out which items actually
 * changed, marks only those as the user's own, and stores the result as a new
 * version of the base resume. The page then re-renders with that version, and
 * the editor (keyed on it) starts over from what was stored.
 */

type ListSection = "work" | "education" | "projects";
type Item<S extends ListSection> = Resume[S][number];

export function ProfileEditor({ initial }: { initial: Resume }) {
  const router = useRouter();
  const [resume, setResume] = useState<Resume>(initial);
  // Bumped on discard, so editors holding a draft of their own start over.
  const [resetKey, setResetKey] = useState(0);
  const [pending, startTransition] = useTransition();
  const pendingFocus = useRef<string | null>(null);

  const knownIds = useMemo(
    () =>
      new Set(
        [...initial.work, ...initial.education, ...initial.projects].map((item) => item.id),
      ),
    [initial],
  );

  const dirty = !sameContent(resume, initial);

  // A newly added item takes focus at its first field.
  useEffect(() => {
    const id = pendingFocus.current;
    if (!id) return;
    pendingFocus.current = null;
    const field = document.querySelector<HTMLInputElement>(`[data-item="${id}"] input`);
    field?.focus();
  });

  function patch<S extends ListSection>(section: S, id: string, change: Partial<Item<S>>) {
    setResume((current) => ({
      ...current,
      [section]: (current[section] as Array<Item<S>>).map((item) =>
        item.id === id ? { ...item, ...change } : item,
      ),
    }));
  }

  function remove(section: ListSection, id: string) {
    setResume((current) => ({
      ...current,
      [section]: (current[section] as Array<{ id: string }>).filter((item) => item.id !== id),
    }));
  }

  function add<S extends ListSection>(section: S, item: Item<S>) {
    pendingFocus.current = item.id;
    // Newest first, the way a resume reads — and next to the button that made it.
    setResume((current) => ({ ...current, [section]: [item, ...current[section]] }));
  }

  function setBasics(change: Partial<Resume["basics"]>) {
    setResume((current) => ({ ...current, basics: { ...current.basics, ...change } }));
  }

  function save() {
    if (!dirty || pending) return;
    const cleaned = prune(resume);
    startTransition(async () => {
      const result = await call(api.PUT("/api/resume", { body: cleaned }));
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setResume(cleaned);
      toast.success(result.message, {
        description: "Scoring, reviews and tailoring use this version from now on.",
      });
      router.refresh();
    });
  }

  function discard() {
    setResume(initial);
    setResetKey((key) => key + 1);
  }

  useUnsavedChanges(dirty, save);

  const links = resume.basics.links;

  return (
    <div className="space-y-10">
      <SubSection id="basics" title="Basics">
        <div className="divide-y rounded-xl border">
          <div className="grid gap-4 p-5 sm:grid-cols-2">
            <Field
              autoComplete="name"
              label="Name"
              onChange={(name) => setBasics({ name: name ?? "" })}
              optional={false}
              value={resume.basics.name}
            />
            <Field
              label="Headline"
              onChange={(headline) => setBasics({ headline })}
              placeholder="Backend engineer, payments and data"
              value={resume.basics.headline}
            />
            <Field
              autoComplete="email"
              label="Email"
              onChange={(email) => setBasics({ email })}
              type="email"
              value={resume.basics.email}
            />
            <Field
              autoComplete="tel"
              label="Phone"
              onChange={(phone) => setBasics({ phone })}
              type="tel"
              value={resume.basics.phone}
            />
            <Field
              className="sm:col-span-2"
              label="Location"
              onChange={(location) => setBasics({ location })}
              placeholder="City, region"
              value={resume.basics.location}
            />
          </div>

          <div className="space-y-2.5 p-5">
            <div className="flex items-center justify-between gap-3">
              <p className="text-muted-foreground text-[12.5px]">Links</p>
              <Button
                onClick={() => {
                  const id = crypto.randomUUID();
                  pendingFocus.current = id;
                  setBasics({ links: [...links, { id, source: "user", label: "", url: "" }] });
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                <Plus />
                Add a link
              </Button>
            </div>
            {links.length === 0 ? (
              <p className="text-muted-foreground text-[13px]">
                No links yet. A portfolio, GitHub or LinkedIn profile helps.
              </p>
            ) : (
              <ul className="space-y-2">
                {links.map((link) => (
                  <li className="flex items-center gap-2" data-item={link.id} key={link.id}>
                    <Input
                      aria-label="Link label"
                      className="h-9 w-28 shrink-0 text-[13.5px] sm:w-40 md:text-[13.5px]"
                      onChange={(event) =>
                        setBasics({
                          links: links.map((item) =>
                            item.id === link.id ? { ...item, label: event.target.value } : item,
                          ),
                        })
                      }
                      placeholder="GitHub"
                      value={link.label}
                    />
                    <Input
                      aria-label="Link address"
                      className="h-9 min-w-0 flex-1 text-[13.5px] md:text-[13.5px]"
                      onChange={(event) =>
                        setBasics({
                          links: links.map((item) =>
                            item.id === link.id ? { ...item, url: event.target.value } : item,
                          ),
                        })
                      }
                      placeholder="github.com/you"
                      value={link.url}
                    />
                    <Button
                      aria-label={`Remove ${link.label || link.url || "this link"}`}
                      className="text-muted-foreground"
                      onClick={() =>
                        setBasics({ links: links.filter((item) => item.id !== link.id) })
                      }
                      size="icon-sm"
                      type="button"
                      variant="ghost"
                    >
                      <X />
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="p-5">
            <AreaField
              label="Summary"
              onChange={(summary) => setResume((current) => ({ ...current, summary }))}
              placeholder="Two or three sentences on what you do best."
              value={resume.summary}
            />
          </div>
        </div>
      </SubSection>

      <SubSection
        action={
          <AddButton
            label="Add a role"
            onClick={() =>
              add("work", {
                id: crypto.randomUUID(),
                source: "user",
                company: "",
                position: "",
                location: null,
                startDate: null,
                endDate: null,
                summary: null,
                highlights: [],
              })
            }
          />
        }
        count={resume.work.length}
        id="experience"
        title="Experience"
      >
        {resume.work.length === 0 ? (
          <EmptyList>No roles yet. Internships and part-time work count.</EmptyList>
        ) : (
          <ol className="divide-y rounded-xl border">
            {resume.work.map((item) => (
              <li className="space-y-4 px-5 pt-5 pb-3" data-item={item.id} key={item.id}>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field
                    label="Title"
                    onChange={(position) => patch("work", item.id, { position: position ?? "" })}
                    optional={false}
                    value={item.position}
                  />
                  <Field
                    label="Company"
                    onChange={(company) => patch("work", item.id, { company: company ?? "" })}
                    optional={false}
                    value={item.company}
                  />
                </div>
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-[1fr_1fr_1.5fr]">
                  <Field
                    label="Started"
                    maxLength={40}
                    onChange={(startDate) => patch("work", item.id, { startDate })}
                    placeholder="June 2024"
                    value={item.startDate}
                  />
                  <Field
                    label="Ended"
                    maxLength={40}
                    onChange={(endDate) => patch("work", item.id, { endDate })}
                    placeholder="Present"
                    value={item.endDate}
                  />
                  <Field
                    className="col-span-2 sm:col-span-1"
                    label="Location"
                    onChange={(location) => patch("work", item.id, { location })}
                    placeholder="City, or remote"
                    value={item.location}
                  />
                </div>
                {item.summary != null && (
                  <AreaField
                    label="Summary"
                    onChange={(summary) => patch("work", item.id, { summary })}
                    value={item.summary}
                  />
                )}
                <BulletList
                  label="Highlights"
                  onChange={(highlights) => patch("work", item.id, { highlights })}
                  value={item.highlights}
                />
                <ItemFooter
                  isNew={!knownIds.has(item.id)}
                  onRemove={() => remove("work", item.id)}
                  removeLabel="Remove role"
                  source={item.source}
                />
              </li>
            ))}
          </ol>
        )}
      </SubSection>

      <SubSection
        action={
          <AddButton
            label="Add a school"
            onClick={() =>
              add("education", {
                id: crypto.randomUUID(),
                source: "user",
                institution: "",
                area: null,
                studyType: null,
                startDate: null,
                endDate: null,
                score: null,
                highlights: [],
              })
            }
          />
        }
        count={resume.education.length}
        id="education"
        title="Education"
      >
        {resume.education.length === 0 ? (
          <EmptyList>No education yet. Degrees, bootcamps and certificates all belong here.</EmptyList>
        ) : (
          <ol className="divide-y rounded-xl border">
            {resume.education.map((item) => (
              <li className="space-y-4 px-5 pt-5 pb-3" data-item={item.id} key={item.id}>
                <div className="grid gap-4 sm:grid-cols-[1.6fr_1fr]">
                  <Field
                    label="School"
                    onChange={(institution) =>
                      patch("education", item.id, { institution: institution ?? "" })
                    }
                    optional={false}
                    value={item.institution}
                  />
                  <Field
                    label="Degree"
                    onChange={(studyType) => patch("education", item.id, { studyType })}
                    placeholder="B.S."
                    value={item.studyType}
                  />
                </div>
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-[1.6fr_1fr_1fr]">
                  <Field
                    className="col-span-2 sm:col-span-1"
                    label="Field of study"
                    onChange={(area) => patch("education", item.id, { area })}
                    placeholder="Computer Science"
                    value={item.area}
                  />
                  <Field
                    label="Started"
                    maxLength={40}
                    onChange={(startDate) => patch("education", item.id, { startDate })}
                    value={item.startDate}
                  />
                  <Field
                    label="Finished"
                    maxLength={40}
                    onChange={(endDate) => patch("education", item.id, { endDate })}
                    value={item.endDate}
                  />
                </div>
                <Field
                  className="sm:max-w-[50%]"
                  label="Grade"
                  onChange={(score) => patch("education", item.id, { score })}
                  placeholder="GPA 3.6, First class honours"
                  value={item.score}
                />
                <BulletList
                  addLabel="Add a detail"
                  label="Details"
                  onChange={(highlights) => patch("education", item.id, { highlights })}
                  placeholder="Honours, activities, relevant coursework"
                  value={item.highlights}
                />
                <ItemFooter
                  isNew={!knownIds.has(item.id)}
                  onRemove={() => remove("education", item.id)}
                  removeLabel="Remove school"
                  source={item.source}
                />
              </li>
            ))}
          </ol>
        )}
      </SubSection>

      <SubSection
        action={
          <AddButton
            label="Add a project"
            onClick={() =>
              add("projects", {
                id: crypto.randomUUID(),
                source: "user",
                name: "",
                description: null,
                url: null,
                startDate: null,
                endDate: null,
                highlights: [],
              })
            }
          />
        }
        count={resume.projects.length}
        id="projects"
        title="Projects"
      >
        {resume.projects.length === 0 ? (
          <EmptyList>No projects yet. Side projects count, especially early in a career.</EmptyList>
        ) : (
          <ol className="divide-y rounded-xl border">
            {resume.projects.map((item) => (
              <li className="space-y-4 px-5 pt-5 pb-3" data-item={item.id} key={item.id}>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field
                    label="Name"
                    onChange={(name) => patch("projects", item.id, { name: name ?? "" })}
                    optional={false}
                    value={item.name}
                  />
                  <Field
                    label="Link"
                    onChange={(url) => patch("projects", item.id, { url })}
                    placeholder="github.com/you/project"
                    value={item.url}
                  />
                </div>
                <AreaField
                  label="Description"
                  onChange={(description) => patch("projects", item.id, { description })}
                  placeholder="What it is and why you built it."
                  value={item.description}
                />
                <div className="grid grid-cols-2 gap-4 sm:max-w-[66%]">
                  <Field
                    label="Started"
                    maxLength={40}
                    onChange={(startDate) => patch("projects", item.id, { startDate })}
                    value={item.startDate}
                  />
                  <Field
                    label="Ended"
                    maxLength={40}
                    onChange={(endDate) => patch("projects", item.id, { endDate })}
                    value={item.endDate}
                  />
                </div>
                <BulletList
                  label="Highlights"
                  onChange={(highlights) => patch("projects", item.id, { highlights })}
                  value={item.highlights}
                />
                <ItemFooter
                  isNew={!knownIds.has(item.id)}
                  onRemove={() => remove("projects", item.id)}
                  removeLabel="Remove project"
                  source={item.source}
                />
              </li>
            ))}
          </ol>
        )}
      </SubSection>

      <SubSection count={resume.skills.length} id="skills" title="Skills">
        <SkillsEditor
          key={resetKey}
          onChange={(skills) => setResume((current) => ({ ...current, skills }))}
          skills={resume.skills}
        />
      </SubSection>

      <SaveBar
        message="Unsaved changes to your resume."
        onDiscard={discard}
        onSave={save}
        pending={pending}
        visible={dirty}
      />
    </div>
  );
}

function SubSection({
  id,
  title,
  count,
  action,
  children,
}: {
  id: string;
  title: string;
  count?: number;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={`${id}-title`} className="scroll-mt-20 space-y-3 lg:scroll-mt-8" id={id}>
      <div className="flex min-h-8 items-center justify-between gap-3">
        <h3 className="text-[15px] font-semibold" id={`${id}-title`}>
          {title}
          {count != null && count > 0 && (
            <span className="text-muted-foreground tabular ml-2 text-[13px] font-normal">{count}</span>
          )}
        </h3>
        {action}
      </div>
      {children}
    </section>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button onClick={onClick} size="sm" type="button" variant="outline">
      <Plus />
      {label}
    </Button>
  );
}

function EmptyList({ children }: { children: ReactNode }) {
  return (
    <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
      {children}
    </p>
  );
}

function ItemFooter({
  source,
  isNew,
  onRemove,
  removeLabel,
}: {
  source: string;
  isNew: boolean;
  onRemove: () => void;
  removeLabel: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <SourceNote isNew={isNew} source={source} />
      <Button
        className="text-muted-foreground"
        onClick={onRemove}
        size="sm"
        type="button"
        variant="ghost"
      >
        <Trash />
        {removeLabel}
      </Button>
    </div>
  );
}

/**
 * Drops what an edit left empty — a role added and never filled in, a blank
 * bullet, a link with no address — so saving never writes placeholders into
 * the facts every model reads.
 */
function prune(resume: Resume): Resume {
  const bullets = (items: string[]) => items.filter((line) => line.trim() !== "");
  const filled = (...values: Array<string | null | undefined>) =>
    values.some((value) => value != null && value.trim() !== "");

  return {
    ...resume,
    basics: {
      ...resume.basics,
      links: resume.basics.links
        .filter((link) => filled(link.url, link.label))
        .map((link) => ({ ...link, label: link.label.trim() || link.url.trim() })),
    },
    work: resume.work
      .map((item) => ({ ...item, highlights: bullets(item.highlights) }))
      .filter((item) => filled(item.company, item.position) || item.highlights.length > 0),
    education: resume.education
      .map((item) => ({ ...item, highlights: bullets(item.highlights) }))
      .filter((item) => filled(item.institution, item.area, item.studyType)),
    projects: resume.projects
      .map((item) => ({ ...item, highlights: bullets(item.highlights) }))
      .filter((item) => filled(item.name, item.description) || item.highlights.length > 0),
    skills: resume.skills.filter((skill) => skill.name.trim() !== ""),
  };
}
