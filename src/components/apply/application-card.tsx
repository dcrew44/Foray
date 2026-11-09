"use client";

import { ChevronDown, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { CompanyMark, ScoreChip, VerdictPill } from "@/components/foray/primitives";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { api, call } from "@/lib/api/client";
import {
  answerText,
  formName,
  missingAnswers,
  plannedAnswers,
  sourceLabel,
  type ApplyAnswer,
  type ApplyItem,
} from "@/lib/apply";
import { formatLocation, plural, postedLabel, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

type Action = "dismiss" | "opened" | "submitted" | "reopen";

/**
 * One prepared application: the posting, what will go into its form, what is
 * still missing, and what the user can do. Nothing here sends anything to an
 * employer: "Open the form" opens the employer's page, and the user submits.
 */
export function ApplicationCard({ item }: { item: ApplyItem }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [open, setOpen] = useState(false);
  const missing = missingAnswers(item.answers);
  const planned = plannedAnswers(item.answers);
  const up = postedLabel(item.postedAt) ?? `First seen ${timeAgo(item.firstSeenAt)}`;

  function act(action: Action, quiet = false) {
    startTransition(async () => {
      const result = await call(
        api.PATCH("/api/applications/{id}", {
          params: { path: { id: item.id } },
          body: { action },
        }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      if (!quiet) toast.success(result.message);
      router.refresh();
    });
  }

  return (
    <article className="bg-card rounded-xl border">
      <div className="flex gap-3.5 px-5 pt-4 pb-3.5">
        <CompanyMark name={item.company} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Link
              className="min-w-0 truncate text-[15px] font-semibold hover:underline"
              href={`/matches/${item.matchId}`}
            >
              {item.title}
            </Link>
            <VerdictPill recommendation={item.recommendation} reviewed={item.deepScore != null} />
          </div>
          <p className="text-muted-foreground mt-0.5 truncate text-[13px]">
            {item.company}
            <span className="mx-1.5" aria-hidden>
              /
            </span>
            {formatLocation(item.locations, item.remote)}
          </p>
          <p className="text-muted-foreground mt-1 text-[12.5px]">{up}</p>
        </div>
        <ScoreChip
          evaluated={item.deepScore != null}
          score={item.deepScore ?? item.fastScore}
          size="md"
        />
      </div>

      {item.status === "failed" ? (
        <p className="text-muted-foreground border-t px-5 py-3 text-[13px]">{item.error}</p>
      ) : (
        <div className="border-t px-5 py-3">
          <button
            aria-expanded={open}
            className="text-muted-foreground hover:text-foreground flex w-full items-center justify-between gap-3 text-left text-[13px]"
            onClick={() => setOpen((value) => !value)}
            type="button"
          >
            <span>
              {plural(planned.length, "answer")} ready
              {item.formRead
                ? `, read from the ${formName(item.formSource)} form`
                : `. The form on ${formName(item.formSource)} may ask more`}
            </span>
            <ChevronDown
              aria-hidden
              className={cn("size-4 shrink-0 transition-transform", open && "rotate-180")}
            />
          </button>
          {open && (
            <dl className="mt-3 divide-y text-[13px]">
              {planned.map((answer) => (
                <div className="grid gap-x-6 gap-y-0.5 py-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]" key={answer.key}>
                  <dt className="text-muted-foreground min-w-0">{answer.label}</dt>
                  <dd className="min-w-0">
                    <span className="break-words whitespace-pre-wrap">{answerText(answer)}</span>
                    <span className="text-muted-foreground block text-[12px]">
                      {sourceLabel(answer.source)}
                    </span>
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}

      {missing.length > 0 && (
        <div className="space-y-4 border-t px-5 py-4">
          <p className="text-[13px] font-medium">
            {missing.length === 1
              ? "One question needs your answer"
              : `${missing.length} questions need your answer`}
          </p>
          {missing.map((answer) => (
            <QuestionForm answer={answer} applicationId={item.id} key={answer.key} />
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t px-5 py-3">
        {item.status === "failed" ? (
          <Button disabled={pending} onClick={() => act("reopen")} size="sm" variant="outline">
            {pending && <Spinner />}
            Try again
          </Button>
        ) : (
          <>
            <Button
              nativeButton={false}
              render={
                <a
                  href={item.applyUrl}
                  onClick={() => act("opened", true)}
                  rel="noreferrer"
                  target="_blank"
                />
              }
              size="sm"
              variant={item.status === "ready" ? "default" : "outline"}
            >
              <ExternalLink aria-hidden />
              Open the form
            </Button>
            <Button disabled={pending} onClick={() => act("submitted")} size="sm" variant="outline">
              I sent it
            </Button>
          </>
        )}
        <Button
          className="ml-auto"
          disabled={pending}
          onClick={() => act("dismiss")}
          size="sm"
          variant="ghost"
        >
          Dismiss
        </Button>
      </div>
    </article>
  );
}

/**
 * One question an application is missing. The answer is saved for next time
 * (#21): under the question's own words, so the next employer who asks it is
 * answered too.
 */
function QuestionForm({ answer, applicationId }: { answer: ApplyAnswer; applicationId: string }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [value, setValue] = useState("");
  const id = `question-${applicationId}-${answer.key}`;
  const isFile = answer.type === "file";

  function save() {
    if (!value.trim() || pending) return;
    startTransition(async () => {
      const result = await call(
        api.POST("/api/applications/{id}/answers", {
          params: { path: { id: applicationId } },
          body: { key: answer.key, value },
        }),
      );
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      router.refresh();
    });
  }

  return (
    <div className="space-y-1.5">
      <label className="block text-[13px]" htmlFor={id}>
        {answer.label}
      </label>
      {isFile ? (
        <p className="text-muted-foreground text-[12.5px]">
          Foray cannot attach this file yet. Add it on the form itself.
        </p>
      ) : (
        <div className="flex flex-wrap items-start gap-2">
          {answer.options.length > 0 ? (
            <Select onValueChange={(next) => setValue(String(next ?? ""))} value={value}>
              <SelectTrigger className="h-9 min-w-52 max-w-full flex-1 text-[13.5px]" id={id}>
                <SelectValue placeholder="Choose one" />
              </SelectTrigger>
              <SelectContent>
                {answer.options.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : answer.type === "textarea" ? (
            <Textarea
              className="min-h-24 flex-1 text-[13.5px]"
              id={id}
              onChange={(event) => setValue(event.target.value)}
              value={value}
            />
          ) : (
            <Input
              className="h-9 flex-1 text-[13.5px] md:text-[13.5px]"
              id={id}
              onChange={(event) => setValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") save();
              }}
              value={value}
            />
          )}
          <Button disabled={pending || !value.trim()} onClick={save} size="lg" variant="outline">
            {pending && <Spinner />}
            Save answer
          </Button>
        </div>
      )}
    </div>
  );
}
