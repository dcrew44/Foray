"use client";

import { useState, useTransition } from "react";

import { saveProfile } from "@/app/actions/profile";
import { Button } from "@/components/ui/button";
import type { Resume } from "@/lib/resume/schema";

import { AreaField, Field, SectionCard, SourceBadge } from "./field";

/**
 * The profile editor.
 *
 * Extraction is good but not perfect, and the profile is the factual basis for
 * everything the product later claims on the user's behalf — so correcting it
 * has to be easy and the corrections have to stick. The whole resume is held in
 * local state and saved in one go; the server works out which items actually
 * changed and marks only those as the user's own.
 */

type Section = keyof Pick<
  Resume,
  "work" | "education" | "projects" | "skills"
>;

export function ProfileEditor({ initial }: { initial: Resume }) {
  const [resume, setResume] = useState<Resume>(initial);
  const [pending, startTransition] = useTransition();
  const [status, setStatus] = useState<string | null>(null);

  const dirty = JSON.stringify(resume) !== JSON.stringify(initial);

  function patchItem<S extends Section>(
    section: S,
    index: number,
    patch: Partial<Resume[S][number]>,
  ) {
    setResume((current) => {
      const next = [...current[section]] as Resume[S];
      next[index] = { ...next[index], ...patch };
      return { ...current, [section]: next };
    });
  }

  function removeItem(section: Section, index: number) {
    setResume((current) => ({
      ...current,
      [section]: current[section].filter((_, i) => i !== index),
    }));
  }

  function save() {
    setStatus(null);
    startTransition(async () => {
      const result = await saveProfile(resume);
      setStatus(result.ok ? "Saved." : result.error);
    });
  }

  return (
    <div className="space-y-10">
      <div className="bg-background/95 sticky top-0 z-10 flex items-center gap-3 border-b py-3 backdrop-blur">
        <Button disabled={!dirty || pending} onClick={save}>
          {pending ? "Saving…" : "Save changes"}
        </Button>
        {dirty && !pending && (
          <span className="text-muted-foreground text-sm">
            Unsaved changes
          </span>
        )}
        {status && (
          <span
            className={
              status === "Saved."
                ? "text-muted-foreground text-sm"
                : "text-destructive text-sm"
            }
            role="status"
          >
            {status}
          </span>
        )}
      </div>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">Basics</h2>
        <SectionCard>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Name"
              onChange={(name) =>
                setResume((c) => ({ ...c, basics: { ...c.basics, name } }))
              }
              value={resume.basics.name}
            />
            <Field
              label="Headline"
              onChange={(headline) =>
                setResume((c) => ({ ...c, basics: { ...c.basics, headline } }))
              }
              value={resume.basics.headline}
            />
            <Field
              label="Email"
              onChange={(email) =>
                setResume((c) => ({ ...c, basics: { ...c.basics, email } }))
              }
              value={resume.basics.email}
            />
            <Field
              label="Phone"
              onChange={(phone) =>
                setResume((c) => ({ ...c, basics: { ...c.basics, phone } }))
              }
              value={resume.basics.phone}
            />
            <Field
              className="sm:col-span-2"
              label="Location"
              onChange={(location) =>
                setResume((c) => ({ ...c, basics: { ...c.basics, location } }))
              }
              value={resume.basics.location}
            />
          </div>

          <AreaField
            label="Summary"
            onChange={(summary) => setResume((c) => ({ ...c, summary }))}
            rows={3}
            value={resume.summary}
          />
        </SectionCard>
      </section>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">
          Experience{" "}
          <span className="text-muted-foreground font-normal">
            ({resume.work.length})
          </span>
        </h2>
        {resume.work.map((item, index) => (
          <SectionCard
            actions={
              <>
                <SourceBadge source={item.source} />
                <RemoveButton onClick={() => removeItem("work", index)} />
              </>
            }
            key={item.id}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Position"
                onChange={(position) => patchItem("work", index, { position })}
                value={item.position}
              />
              <Field
                label="Company"
                onChange={(company) => patchItem("work", index, { company })}
                value={item.company}
              />
              <Field
                label="Start"
                onChange={(startDate) => patchItem("work", index, { startDate })}
                placeholder="June 2024"
                value={item.startDate}
              />
              <Field
                label="End"
                onChange={(endDate) => patchItem("work", index, { endDate })}
                placeholder="Present"
                value={item.endDate}
              />
              <Field
                className="sm:col-span-2"
                label="Location"
                onChange={(location) => patchItem("work", index, { location })}
                value={item.location}
              />
            </div>
            <AreaField
              hint="one per line"
              label="Bullets"
              onChange={(text) =>
                patchItem("work", index, { highlights: splitLines(text) })
              }
              rows={Math.max(item.highlights.length + 1, 3)}
              value={item.highlights.join("\n")}
            />
          </SectionCard>
        ))}
      </section>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">
          Education{" "}
          <span className="text-muted-foreground font-normal">
            ({resume.education.length})
          </span>
        </h2>
        {resume.education.map((item, index) => (
          <SectionCard
            actions={
              <>
                <SourceBadge source={item.source} />
                <RemoveButton onClick={() => removeItem("education", index)} />
              </>
            }
            key={item.id}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Institution"
                onChange={(institution) =>
                  patchItem("education", index, { institution })
                }
                value={item.institution}
              />
              <Field
                label="Degree"
                onChange={(studyType) =>
                  patchItem("education", index, { studyType })
                }
                placeholder="B.S."
                value={item.studyType}
              />
              <Field
                label="Field"
                onChange={(area) => patchItem("education", index, { area })}
                value={item.area}
              />
              <Field
                label="Finished"
                onChange={(endDate) =>
                  patchItem("education", index, { endDate })
                }
                value={item.endDate}
              />
            </div>
          </SectionCard>
        ))}
      </section>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">
          Projects{" "}
          <span className="text-muted-foreground font-normal">
            ({resume.projects.length})
          </span>
        </h2>
        {resume.projects.map((item, index) => (
          <SectionCard
            actions={
              <>
                <SourceBadge source={item.source} />
                <RemoveButton onClick={() => removeItem("projects", index)} />
              </>
            }
            key={item.id}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Name"
                onChange={(name) => patchItem("projects", index, { name })}
                value={item.name}
              />
              <Field
                label="Link"
                onChange={(url) => patchItem("projects", index, { url })}
                value={item.url}
              />
            </div>
            <AreaField
              label="Description"
              onChange={(description) =>
                patchItem("projects", index, { description })
              }
              rows={2}
              value={item.description}
            />
            <AreaField
              hint="one per line"
              label="Bullets"
              onChange={(text) =>
                patchItem("projects", index, { highlights: splitLines(text) })
              }
              rows={Math.max(item.highlights.length + 1, 2)}
              value={item.highlights.join("\n")}
            />
          </SectionCard>
        ))}
      </section>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">
          Skills{" "}
          <span className="text-muted-foreground font-normal">
            ({resume.skills.length})
          </span>
        </h2>
        <SectionCard>
          <div className="grid gap-2 sm:grid-cols-2">
            {resume.skills.map((item, index) => (
              <div className="flex items-center gap-2" key={item.id}>
                <Field
                  className="flex-1"
                  label="Skill"
                  onChange={(name) => patchItem("skills", index, { name })}
                  value={item.name}
                />
                <Field
                  className="w-40"
                  label="Group"
                  onChange={(category) =>
                    patchItem("skills", index, { category })
                  }
                  value={item.category}
                />
                <RemoveButton onClick={() => removeItem("skills", index)} />
              </div>
            ))}
          </div>
        </SectionCard>
      </section>
    </div>
  );
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  return (
    <Button
      aria-label="Remove"
      className="shrink-0 self-end"
      onClick={onClick}
      size="sm"
      type="button"
      variant="ghost"
    >
      Remove
    </Button>
  );
}

/** Blank lines are formatting, not empty bullets. */
function splitLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}
