import { ProfileEditor } from "@/components/profile/profile-editor";
import { ResumeUpload } from "@/components/profile/resume-upload";
import { requireUser } from "@/lib/auth/session";
import { getProfile } from "@/lib/db/queries/profile";
import { ResumeSchema, type Resume } from "@/lib/resume/schema";

export default async function ProfilePage() {
  const user = await requireUser();
  const profile = await getProfile(user.id);

  // Stored profiles are re-parsed rather than cast: the schema has grown since
  // Step 8 and will grow again, and a row written by an older shape should fill
  // in defaults rather than crash the page.
  const parsed = profile ? ResumeSchema.safeParse(profile.data) : null;
  const resume: Resume | null = parsed?.success ? parsed.data : null;

  return (
    <main className="mx-auto w-full max-w-3xl space-y-8 p-8">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Profile</h1>
        <p className="text-muted-foreground text-sm">
          Everything Foray knows about you. This is what job matches are scored
          against and what tailored resumes are built from, so it is worth
          getting right.
        </p>
      </div>

      <ResumeUpload hasProfile={Boolean(resume)} />

      {parsed && !parsed.success && (
        <p className="text-destructive text-sm" role="alert">
          Your stored profile could not be read ({parsed.error.issues[0]?.message}).
          Upload your resume again to rebuild it.
        </p>
      )}

      {resume ? (
        <ProfileEditor initial={resume} />
      ) : (
        <p className="text-muted-foreground text-sm">
          No profile yet. Upload a resume and it will be read into editable
          sections here.
        </p>
      )}
    </main>
  );
}
