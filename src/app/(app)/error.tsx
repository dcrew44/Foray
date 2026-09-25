"use client";

import { RotateCcw } from "lucide-react";
import Link from "next/link";

import { PageBody } from "@/components/foray/primitives";
import { Button, buttonVariants } from "@/components/ui/button";

/** A page that failed to render, said plainly, with a way forward. */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <PageBody width="narrow">
      <div className="space-y-4 py-10">
        <h1 className="text-display text-[28px]">This page did not load.</h1>
        <p className="text-muted-foreground max-w-[56ch] text-[14.5px] leading-relaxed">
          Something failed while fetching it — usually the database or the dev server
          restarting. Your data is safe. {error.digest ? `(Reference ${error.digest}.)` : ""}
        </p>
        <div className="flex gap-2">
          <Button onClick={reset}>
            <RotateCcw />
            Try again
          </Button>
          <Link className={buttonVariants({ variant: "outline" })} href="/home">
            Go home
          </Link>
        </div>
      </div>
    </PageBody>
  );
}
