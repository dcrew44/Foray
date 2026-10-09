import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { SignInForm } from "@/components/auth/sign-in-form";
import { ScoreChip, Wordmark } from "@/components/foray/primitives";
import type { components } from "@/lib/api/schema";
import { django, getMeOrNull } from "@/lib/api/server";

export const metadata: Metadata = { title: "Sign in" };

/** Only same-site paths are honoured, so a crafted link cannot bounce elsewhere. */
function safeNext(next: string | undefined): string {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/home";
}

/**
 * Signed in: straight on to `next`. Signed out: the form. `getMeOrNull()` answers null on a 401
 * instead of redirecting, so a signed-out visit here can never loop back to itself.
 */
export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const next = safeNext((await searchParams).next);
  if (await getMeOrNull()) redirect(next);

  const { signupOpen } = await django<components["schemas"]["ConfigOut"]>("/api/auth/config");

  return (
    <div className="bg-canvas grid min-h-svh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <div className="bg-background flex flex-col px-6 py-8 sm:px-12 lg:rounded-r-3xl lg:border-r">
        <Wordmark />
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center py-12">
          <SignInForm next={next} signupOpen={signupOpen} />
        </div>
        <p className="text-muted-foreground text-[12.5px]">
          Your resume and matches stay on this server. Nothing is shared.
        </p>
      </div>

      <div className="hidden flex-col justify-center px-14 py-16 lg:flex xl:px-20">
        <Vignette />
      </div>
    </div>
  );
}

/**
 * What the product does, shown rather than described: an agent's afternoon,
 * compressed. Illustrative companies, not real listings.
 */
function Vignette() {
  const rows = [
    { score: 91, title: "Senior Backend Engineer, Payments", company: "Kestrel Labs", note: "Owns a billing service end to end, exactly your last two years." },
    { score: 84, title: "Staff Software Engineer, Platform", company: "Harbor Analytics", note: "Strong on the migration work; asks for more Go than you list." },
    { score: 66, title: "Backend Engineer II", company: "Tidewater Health", note: "Good stack match, but a level below what you asked for." },
  ];

  return (
    <div className="max-w-xl">
      <p className="text-display text-[40px] leading-[1.05] xl:text-[46px]">
        Foray reads every posting, so you only read the good ones.
      </p>
      <p className="text-muted-foreground mt-5 max-w-[48ch] text-[15px] leading-relaxed">
        Tell it what you want in plain words. It searches hundreds of company
        boards, scores each role against your actual resume, and tailors your
        resume for the ones worth applying to.
      </p>

      <div className="bg-background mt-10 overflow-hidden rounded-2xl border shadow-[0_18px_50px_-24px_rgb(23_34_29/0.25)]">
        <div className="text-muted-foreground flex items-center justify-between border-b px-5 py-3 text-[12.5px]">
          <span>Search finished a minute ago</span>
          <span className="tabular">2,418 postings read</span>
        </div>
        <ul className="divide-y">
          {rows.map((row) => (
            <li className="flex items-start gap-4 px-5 py-4" key={row.title}>
              <ScoreChip score={row.score} />
              <div className="min-w-0">
                <p className="truncate text-[14px] font-medium">{row.title}</p>
                <p className="text-muted-foreground text-[13px]">{row.company}</p>
                <p className="mt-1.5 text-[13px] leading-snug">{row.note}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
