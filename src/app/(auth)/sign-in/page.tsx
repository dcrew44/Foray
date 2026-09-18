import { redirect } from "next/navigation";

import { SignInForm } from "@/components/auth/sign-in-form";
import { getSession } from "@/lib/auth/session";
import { env } from "@/env";

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;
  if (await getSession()) redirect(next ?? "/chat");

  const googleEnabled = Boolean(
    env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET,
  );

  return (
    <main className="mx-auto flex min-h-svh w-full max-w-sm flex-col justify-center gap-8 p-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Foray</h1>
        <p className="text-muted-foreground text-sm">
          Sign in to pick up your search.
        </p>
      </div>
      <SignInForm googleEnabled={googleEnabled} next={next ?? "/chat"} />
    </main>
  );
}
