"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { toast } from "sonner";

import { Section } from "@/components/foray/primitives";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { plural } from "@/lib/format";

type MatchApplication = components["schemas"]["MatchApplication"];

const STATE: Record<MatchApplication["status"], string> = {
  preparing: "Foray is reading the application form.",
  needs_answers: "Planned, and waiting on your answers.",
  ready: "Ready to send: every required field has its answer.",
  submitted: "You sent this application.",
  dismissed: "You dismissed this application.",
  failed: "The application form could not be read.",
};

/**
 * Where this match's application stands (#60), and the one thing to do next:
 * prepare it, answer what it is missing on the Apply page, or send it there.
 */
export function ApplicationPanel({
  matchId,
  application,
  closed,
}: {
  matchId: string;
  application: MatchApplication | null;
  closed: boolean;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  function prepare() {
    startTransition(async () => {
      const result = application
        ? await call(
            api.PATCH("/api/applications/{id}", {
              params: { path: { id: application.id } },
              body: { action: "reopen" },
            }),
          )
        : await call(api.POST("/api/applications", { body: { matchId } }));
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      router.refresh();
    });
  }

  if (!application && closed) return null;
  const again = application?.status === "dismissed" || application?.status === "failed";

  return (
    <Section title="Application">
      <div className="space-y-3 text-[13px]">
        <p className="text-muted-foreground leading-snug">
          {application
            ? application.status === "needs_answers"
              ? `Planned, and waiting on ${plural(application.missing, "answer")} from you.`
              : STATE[application.status]
            : "Foray can read this posting's form and plan your answers from your resume and saved answers. It sends nothing."}
        </p>
        {!application || again ? (
          <Button disabled={pending} onClick={prepare} size="sm" variant="outline">
            {pending && <Spinner />}
            {again ? "Prepare it again" : "Prepare application"}
          </Button>
        ) : application.status === "needs_answers" || application.status === "ready" ? (
          <Button
            nativeButton={false}
            render={<Link href="/apply" />}
            size="sm"
            variant={application.status === "ready" ? "default" : "outline"}
          >
            {application.status === "ready" ? "Review and send" : "Answer on the Apply page"}
          </Button>
        ) : null}
      </div>
    </Section>
  );
}
