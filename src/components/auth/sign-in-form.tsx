"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { signIn, signUp } from "@/lib/auth/client";

type Mode = "sign-in" | "sign-up";

export function SignInForm({
  googleEnabled,
  next,
}: {
  googleEnabled: boolean;
  next: string;
}) {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("sign-in");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const result =
      mode === "sign-up"
        ? await signUp.email({ name: name || email.split("@")[0]!, email, password })
        : await signIn.email({ email, password });

    if (result.error) {
      setPending(false);
      setError(
        result.error.message ??
          (mode === "sign-up"
            ? "That account could not be created."
            : "That email and password do not match an account."),
      );
      return;
    }

    // A brand-new account starts at onboarding, not an empty dashboard.
    router.push(mode === "sign-up" ? "/home" : next);
    router.refresh();
  }

  return (
    <div className="space-y-7">
      <div className="space-y-2">
        <h1 className="text-display text-[30px] leading-tight">
          {mode === "sign-in" ? "Welcome back" : "Start your search"}
        </h1>
        <p className="text-muted-foreground text-[14.5px]">
          {mode === "sign-in"
            ? "Sign in to see what your agents found."
            : "Create an account, upload your resume, and Foray takes it from there."}
        </p>
      </div>

      <form className="space-y-4" onSubmit={onSubmit}>
        {mode === "sign-up" && (
          <div className="space-y-1.5">
            <Label htmlFor="name">Name</Label>
            <Input
              autoComplete="name"
              className="h-10"
              id="name"
              onChange={(e) => setName(e.target.value)}
              value={name}
            />
          </div>
        )}
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input
            autoComplete="email"
            className="h-10"
            id="email"
            onChange={(e) => setEmail(e.target.value)}
            required
            type="email"
            value={email}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input
            autoComplete={mode === "sign-up" ? "new-password" : "current-password"}
            className="h-10"
            id="password"
            minLength={8}
            onChange={(e) => setPassword(e.target.value)}
            required
            type="password"
            value={password}
          />
          {mode === "sign-up" && (
            <p className="text-muted-foreground text-[12.5px]">At least 8 characters.</p>
          )}
        </div>

        {error && (
          <p className="text-destructive text-[13px]" role="alert">
            {error}
          </p>
        )}

        <Button className="h-10 w-full text-[14px]" disabled={pending} type="submit">
          {pending
            ? mode === "sign-up"
              ? "Creating your account…"
              : "Signing in…"
            : mode === "sign-up"
              ? "Create account"
              : "Sign in"}
        </Button>
      </form>

      {googleEnabled && (
        <Button
          className="h-10 w-full"
          onClick={() => signIn.social({ provider: "google", callbackURL: next })}
          type="button"
          variant="outline"
        >
          Continue with Google
        </Button>
      )}

      <p className="text-muted-foreground text-[13.5px]">
        {mode === "sign-in" ? "New to Foray? " : "Already have an account? "}
        <button
          className="text-primary font-medium underline-offset-4 hover:underline"
          onClick={() => {
            setMode(mode === "sign-in" ? "sign-up" : "sign-in");
            setError(null);
          }}
          type="button"
        >
          {mode === "sign-in" ? "Create an account" : "Sign in"}
        </button>
      </p>
    </div>
  );
}
