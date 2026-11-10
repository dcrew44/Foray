"use client";

import { MailCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { toast } from "sonner";

import { Section, SignalDot } from "@/components/foray/primitives";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { confirmationLine, suggestionQuestion } from "@/lib/mail";

type MatchMail = components["schemas"]["MatchMail"];

/**
 * What the employer's email says about this match (#26): the confirmation that the
 * application arrived, and the newest later email that looks like a rejection or an invitation.
 * Such an email only suggests a stage. Move is the one thing that changes it, through the same
 * stage change the picker above makes; Dismiss puts the suggestion away for good. Nothing is
 * shown for a match no email was matched to.
 */
export function MatchMailPanel({ mail }: { mail: MatchMail }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const { suggestion } = mail;

  function answer(action: "accept" | "dismiss") {
    if (!suggestion) return;
    startTransition(async () => {
      const result = await call(
        api.POST("/api/mail/suggestions/{id}", {
          params: { path: { id: suggestion.id } },
          body: { action },
        }),
      );
      if (!result.ok) toast.error(result.message);
      else toast(result.message);
      // Either way the page is stale: the stage moved, or the suggestion no longer applies.
      router.refresh();
    });
  }

  if (!suggestion && !mail.confirmedAt) return null;

  return (
    <Section title="From the employer">
      <div className="space-y-3 text-[13px]">
        {suggestion && (
          <div className="space-y-3 rounded-xl border px-4 py-3.5">
            <p className="flex gap-2.5 leading-snug">
              <SignalDot className="mt-[0.4em]" />
              <span className="min-w-0 break-words">{suggestionQuestion(suggestion)}</span>
            </p>
            <div className="flex flex-wrap gap-2 pl-[1.125rem]">
              <Button disabled={pending} onClick={() => answer("accept")} size="sm">
                {pending && <Spinner />}
                Move
              </Button>
              <Button
                disabled={pending}
                onClick={() => answer("dismiss")}
                size="sm"
                variant="ghost"
              >
                Dismiss
              </Button>
            </div>
          </div>
        )}
        {mail.confirmedAt && (
          <p className="text-muted-foreground flex gap-2 leading-snug">
            <MailCheck aria-hidden className="text-success mt-px size-4 shrink-0" />
            <span className="min-w-0 break-words">
              {confirmationLine(mail.confirmedAt, mail.confirmationSubject)}
            </span>
          </p>
        )}
      </div>
    </Section>
  );
}
