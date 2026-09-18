import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { requireUser } from "@/lib/auth/session";
import { getProfile } from "@/lib/db/queries/profile";
import {
  EMPTY_INTERVIEW_STATE,
  EMPTY_PREFERENCES,
  InterviewStateSchema,
  PreferencesSchema,
  interviewProgress,
} from "@/lib/profile/interview";
import { ResumeSchema } from "@/lib/resume/schema";

/**
 * What still stands between this user and a first search.
 *
 * Deliberately a checklist and not a wizard: the interview happens in chat,
 * and the profile is edited on its own page. This only answers "what is left",
 * and points at wherever that work actually happens.
 */
export default async function OnboardingPage() {
  const user = await requireUser();
  const profile = await getProfile(user.id);

  const resume = profile ? ResumeSchema.safeParse(profile.data).data : null;
  const hasResume = Boolean(resume && resume.work.length + resume.education.length > 0);

  const preferences =
    PreferencesSchema.safeParse(profile?.preferences).data ?? EMPTY_PREFERENCES;
  const state =
    InterviewStateSchema.safeParse(profile?.interviewState).data ??
    EMPTY_INTERVIEW_STATE;
  const progress = interviewProgress(preferences, state);

  const ready = hasResume && progress.blocking.length === 0;

  return (
    <main className="mx-auto w-full max-w-2xl space-y-8 p-8">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Getting set up</h1>
        <p className="text-muted-foreground text-sm">
          Two things, and then Foray can go looking.
        </p>
      </div>

      <ol className="space-y-4">
        <Step
          action={
            <Link
              className={buttonVariants({
                size: "sm",
                variant: hasResume ? "outline" : "default",
              })}
              href="/profile"
            >
              {hasResume ? "Review profile" : "Upload resume"}
            </Link>
          }
          detail={
            hasResume
              ? `${plural(resume?.work.length ?? 0, "role")}, ${plural(resume?.education.length ?? 0, "education entry", "education entries")} and ${plural(resume?.skills.length ?? 0, "skill")} on file. Worth checking the extraction got them right.`
              : "A PDF or .docx. Everything else is built from this, so it comes first."
          }
          done={hasResume}
          title="Add your resume"
        />

        <Step
          action={
            <Link
              className={buttonVariants({
                size: "sm",
                variant: progress.complete ? "outline" : "default",
              })}
              href="/chat"
            >
              {progress.complete ? "Open chat" : "Answer in chat"}
            </Link>
          }
          detail={
            hasResume
              ? `${progress.answered} of ${progress.total} answered.`
              : "Available once your resume is in."
          }
          done={progress.complete}
          title="Tell Foray what you are looking for"
        >
          {hasResume && progress.outstanding.length > 0 && (
            <ul className="text-muted-foreground mt-2 grid gap-1 text-sm sm:grid-cols-2">
              {progress.outstanding.map((slot) => (
                <li key={slot.id}>
                  {slot.label}
                  {slot.required && (
                    <span className="ml-1 text-xs opacity-70">(needed)</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Step>
      </ol>

      <div className="border-border rounded-lg border p-4 text-sm">
        {ready ? (
          <p>
            Ready to search. Ask for what you want in{" "}
            <Link className="underline underline-offset-4" href="/chat">
              chat
            </Link>{" "}
            — though searching itself lands in a later step.
          </p>
        ) : (
          <p className="text-muted-foreground">
            A search needs at least:{" "}
            {progress.blocking.length > 0
              ? progress.blocking.map((slot) => slot.label).join(", ")
              : "your resume"}
            .
          </p>
        )}
      </div>
    </main>
  );
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function Step({
  title,
  detail,
  done,
  action,
  children,
}: {
  title: string;
  detail: string;
  done: boolean;
  action: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <li className="border-border flex items-start gap-4 rounded-lg border p-4">
      <span
        aria-hidden
        className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border text-xs ${
          done ? "bg-foreground text-background border-transparent" : ""
        }`}
      >
        {done ? "✓" : ""}
      </span>
      <div className="flex-1 space-y-1">
        <p className="font-medium">{title}</p>
        <p className="text-muted-foreground text-sm">{detail}</p>
        {children}
      </div>
      <div className="shrink-0">{action}</div>
    </li>
  );
}
