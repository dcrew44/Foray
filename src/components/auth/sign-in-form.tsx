"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
        ? await signUp.email({ name: name || email, email, password })
        : await signIn.email({ email, password });

    setPending(false);

    if (result.error) {
      setError(result.error.message ?? "Something went wrong.");
      return;
    }

    router.push(next);
    router.refresh();
  }

  return (
    <div className="space-y-6">
      <form className="space-y-3" onSubmit={onSubmit}>
        {mode === "sign-up" && (
          <Input
            aria-label="Name"
            autoComplete="name"
            onChange={(e) => setName(e.target.value)}
            placeholder="Name"
            value={name}
          />
        )}
        <Input
          aria-label="Email"
          autoComplete="email"
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          required
          type="email"
          value={email}
        />
        <Input
          aria-label="Password"
          autoComplete={
            mode === "sign-up" ? "new-password" : "current-password"
          }
          minLength={8}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          required
          type="password"
          value={password}
        />

        {error && (
          <p className="text-destructive text-sm" role="alert">
            {error}
          </p>
        )}

        <Button className="w-full" disabled={pending} type="submit">
          {pending
            ? "Working…"
            : mode === "sign-up"
              ? "Create account"
              : "Sign in"}
        </Button>
      </form>

      {googleEnabled && (
        <Button
          className="w-full"
          onClick={() => signIn.social({ provider: "google", callbackURL: next })}
          type="button"
          variant="outline"
        >
          Continue with Google
        </Button>
      )}

      <button
        className="text-muted-foreground hover:text-foreground w-full text-center text-sm underline-offset-4 hover:underline"
        onClick={() => {
          setMode(mode === "sign-in" ? "sign-up" : "sign-in");
          setError(null);
        }}
        type="button"
      >
        {mode === "sign-in"
          ? "No account yet? Create one"
          : "Already have an account? Sign in"}
      </button>
    </div>
  );
}
