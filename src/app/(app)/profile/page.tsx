import type { Metadata } from "next";

import { PageBody, Section } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import { PageNav, type PageNavItem } from "@/components/profile/page-nav";
import { PreferencesForm } from "@/components/profile/preferences-form";
import { ProfileEditor } from "@/components/profile/profile-editor";
import { ProfileHeader } from "@/components/profile/profile-header";
import { ResumeDropzone } from "@/components/profile/resume-dropzone";
import { requireUser } from "@/lib/auth/session";
import { getBaseResume } from "@/lib/db/queries/profile";
import { latestBaseRender } from "@/lib/db/queries/resumes";
import { timeAgo } from "@/lib/format";
import { getOnboardingState } from "@/lib/profile/onboarding";

export const metadata: Metadata = { title: "Profile" };

/**
 * Everything Foray knows about the user: what they are looking for, and the
 * resume facts every model judges them by. Until the interview is done the
 * preferences come first, since they are what stands between the user and a
 * first search.
 */
export default async function ProfilePage() {
  const user = await requireUser();
  const [onboarding, base, render] = await Promise.all([
    getOnboardingState(user.id),
    getBaseResume(user.id),
    latestBaseRender(user.id),
  ]);
  const { resume, preferences, progress } = onboarding;

  const name = resume?.basics.name.trim();
  const header = (
    <ProfileHeader
      canRender={Boolean(base && resume)}
      description="Everything Foray knows about you. Every score, deep review and tailored resume is judged against these facts, so they are worth getting right."
      pdf={
        render
          ? {
              documentId: render.document.id,
              version: render.resume.version,
              current: render.resume.version === base?.version,
            }
          : null
      }
      title={name || "Your profile"}
    />
  );

  if (!onboarding.hasProfile) {
    return (
      <PageBody>
        {header}
        <div className="mt-10 max-w-2xl">
          <h2 className="text-display text-[20px] leading-tight">Start with your resume</h2>
          <p className="text-muted-foreground mt-2 max-w-[58ch] text-[14px] leading-relaxed">
            Foray reads your roles, schools, projects and skills into a profile you can correct
            here. Then you tell it what you are looking for, and it goes searching.
          </p>
          <ResumeDropzone className="mt-6" hasResume={false} />
        </div>
      </PageBody>
    );
  }

  const blocking = progress.blocking.map((slot) => slot.label.toLowerCase());
  const preferencesFirst = blocking.length > 0;

  const preferencesSection = (
    <div className="scroll-mt-20 lg:scroll-mt-8" id="preferences" key="preferences">
      <Section
        description={
          blocking.length > 0
            ? `Foray needs your ${joinList(blocking)} before it can search. The rest sharpens the ranking.`
            : "Every search, score and deep review starts from these answers. You can also change them by telling Foray in chat."
        }
        title="What you're looking for"
      >
        <PreferencesForm
          canSave={onboarding.hasProfile}
          initial={preferences}
          key={JSON.stringify(preferences)}
        />
      </Section>
    </div>
  );

  const resumeSection = (
    <div className="scroll-mt-20 lg:scroll-mt-8" id="resume" key="resume">
      <Section
        description={
          base
            ? `Version ${base.version}, saved ${timeAgo(base.createdAt)}. Each save keeps the one before, so tailored resumes can still cite the facts they were built from.`
            : "Correct anything the extraction got wrong. Every model that judges you reads this."
        }
        title="Resume"
      >
        <div className="space-y-10">
          <div className="space-y-2">
            <ResumeDropzone compact hasResume />
            <p className="text-muted-foreground px-1 text-[12.5px]">
              A new file is read from scratch, so edits made here are not carried over.
            </p>
          </div>
          {resume ? (
            <ProfileEditor initial={resume} key={base?.version ?? 0} />
          ) : (
            <p className="text-destructive text-[13.5px]" role="alert">
              Your stored resume could not be read. Upload it again to rebuild your profile.
            </p>
          )}
        </div>
      </Section>
    </div>
  );

  const preferencesNav: PageNavItem = {
    id: "preferences",
    label: "What you're looking for",
    detail: blocking.length > 0 ? `${blocking.length} needed` : undefined,
  };
  const resumeNav: PageNavItem[] = [
    { id: "resume", label: "Resume" },
    ...(resume
      ? [
          { id: "basics", label: "Basics", child: true },
          { id: "experience", label: "Experience", child: true, detail: count(resume.work.length) },
          { id: "education", label: "Education", child: true, detail: count(resume.education.length) },
          { id: "projects", label: "Projects", child: true, detail: count(resume.projects.length) },
          { id: "skills", label: "Skills", child: true, detail: count(resume.skills.length) },
        ]
      : []),
  ];

  return (
    <PageBody>
      <RefreshOn kinds={["render.done"]} />
      {header}

      <div className="mt-10 xl:grid xl:grid-cols-[minmax(0,1fr)_13rem] xl:gap-16">
        <div className="min-w-0 max-w-3xl space-y-16">
          {preferencesFirst
            ? [preferencesSection, resumeSection]
            : [resumeSection, preferencesSection]}
        </div>
        <aside className="hidden xl:block">
          <PageNav
            className="sticky top-10"
            items={
              preferencesFirst
                ? [preferencesNav, ...resumeNav]
                : [...resumeNav, preferencesNav]
            }
          />
        </aside>
      </div>
    </PageBody>
  );
}

function count(n: number): string | undefined {
  return n > 0 ? String(n) : undefined;
}

/** "a, b and c" */
function joinList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}
