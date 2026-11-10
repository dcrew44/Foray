"use client";

import { Check, Mail } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { toast } from "sonner";

import { SignalDot } from "@/components/foray/primitives";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { timeAgo } from "@/lib/format";
import { foundSummary, GMAIL_DOC, waitingLabel } from "@/lib/mail";

type MailSettings = components["schemas"]["MailSettings"];

/**
 * The mailbox Foray reads employers' replies from (#26): connect one, see which it is, when it
 * was last checked and what was found, and disconnect it. Reading is the worker's job, every
 * 15 minutes; this panel only starts and ends the access, and says in plain words what the
 * access is used for.
 *
 * Connecting leaves the app: Django answers Google's consent page, Google sends the browser
 * back to Django, and Django sends it here with one word in the address (`?mail=connected`),
 * which the page has already turned into `mail.notice`. The word is taken out of the address
 * once it has been shown, so a reload does not say it again.
 */
export function MailPanel({ mail }: { mail: MailSettings }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [confirming, setConfirming] = useState(false);
  // Kept from the first render: the address is cleaned below, and the sentence should stay.
  const [notice] = useState(mail.notice);
  const { account } = mail;

  useEffect(() => {
    if (!notice) return;
    const url = new URL(window.location.href);
    if (!url.searchParams.has("mail")) return;
    url.searchParams.delete("mail");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }, [notice]);

  function connect() {
    startTransition(async () => {
      const result = await call(api.POST("/api/mail/google/start"));
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      // Off to Google's consent page, and back through Django's callback.
      window.location.assign(result.url);
    });
  }

  function disconnect() {
    startTransition(async () => {
      const result = await call(api.DELETE("/api/mail/google"));
      setConfirming(false);
      if (!result.ok) toast.error(result.message);
      else toast.success(result.message);
      router.refresh();
    });
  }

  const found = account ? foundSummary(account.found) : null;
  const needsReconnect = account?.status === "reconnect";

  return (
    <div className="divide-y rounded-xl border">
      {notice && (
        <div className="px-5 py-3.5 text-[13px] leading-relaxed" role="status">
          {notice.ok ? (
            <p className="flex items-center gap-2">
              <Check className="text-success size-4 shrink-0" />
              {notice.message}
            </p>
          ) : (
            <p className="text-destructive">{notice.message}</p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4">
        <div className="min-w-0 space-y-1">
          {account ? (
            <>
              <p className="flex items-center gap-2 text-[14px] font-medium">
                <SignalDot tone={needsReconnect ? "signal" : "ok"} />
                <span className="truncate">{account.address}</span>
              </p>
              <p className="text-muted-foreground text-[12.5px] leading-snug">
                {needsReconnect
                  ? "Not being checked."
                  : account.lastCheckedAt
                    ? `Last checked ${timeAgo(account.lastCheckedAt)}. Foray looks every 15 minutes while the worker runs.`
                    : "Not checked yet. The worker checks it within a few minutes of connecting, then every 15 minutes."}
              </p>
            </>
          ) : (
            <>
              <p className="flex items-center gap-2 text-[14px] font-medium">
                <SignalDot tone="idle" />
                {mail.configured ? "No mailbox connected" : "Not set up on this install"}
              </p>
              <p className="text-muted-foreground max-w-[60ch] text-[12.5px] leading-snug">
                {mail.configured ? (
                  "Connect Gmail and Foray records when an employer confirms your application arrived, and points out a rejection or an interview invitation on the match it is about."
                ) : (
                  <>
                    Reading mail needs a Google OAuth client of this install&rsquo;s own.
                    Whoever runs it creates one in Google Cloud, sets{" "}
                    <code className="text-foreground">GOOGLE_CLIENT_ID</code> and{" "}
                    <code className="text-foreground">GOOGLE_CLIENT_SECRET</code> in{" "}
                    <code className="text-foreground">.env</code> and restarts the app and the
                    worker.{" "}
                    <a
                      className="text-primary underline-offset-4 hover:underline"
                      href={GMAIL_DOC}
                      rel="noreferrer"
                      target="_blank"
                    >
                      docs/gmail.md
                    </a>{" "}
                    has the steps. The redirect URI to register is{" "}
                    <code className="text-foreground break-all">{mail.redirectUri}</code>.
                  </>
                )}
              </p>
            </>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {mail.configured && (!account || needsReconnect) && (
            <Button disabled={pending} onClick={connect}>
              {pending && !confirming ? <Spinner /> : <Mail />}
              {account ? "Connect again" : "Connect Gmail"}
            </Button>
          )}
          {account && !confirming && (
            <Button disabled={pending} onClick={() => setConfirming(true)} variant="outline">
              Disconnect
            </Button>
          )}
        </div>
      </div>

      {account && confirming && (
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-3.5">
          <p className="text-[13px] leading-snug">
            Disconnect {account.address}? Everything Foray read from it is deleted. Stages you
            moved and confirmations already recorded on applications stay.
          </p>
          <div className="flex shrink-0 gap-2">
            <Button disabled={pending} onClick={disconnect} size="sm">
              {pending && <Spinner />}
              Disconnect
            </Button>
            <Button disabled={pending} onClick={() => setConfirming(false)} size="sm" variant="ghost">
              Keep it
            </Button>
          </div>
        </div>
      )}

      {account && (account.lastError || found || account.waiting.length > 0) && (
        <div className="space-y-3 px-5 py-4 text-[13px] leading-relaxed">
          {account.lastError && (
            <p className={needsReconnect ? "text-foreground" : "text-destructive"} role="status">
              {account.lastError}
            </p>
          )}
          {found && <p className="text-muted-foreground">Found so far: {found}</p>}
          {account.waiting.length > 0 && (
            <div className="space-y-1.5">
              <p className="font-medium">Waiting on you</p>
              <ul className="space-y-1">
                {account.waiting.map((waiting) => (
                  <li className="flex items-baseline gap-2" key={`${waiting.matchId}-${waiting.receivedAt}`}>
                    <SignalDot className="translate-y-[-1px]" />
                    <Link
                      className="text-primary min-w-0 underline-offset-4 hover:underline"
                      href={`/matches/${waiting.matchId}`}
                    >
                      {waitingLabel(waiting)}
                    </Link>
                    <span className="text-muted-foreground shrink-0 text-[12.5px]">
                      {timeAgo(waiting.receivedAt)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <div className="text-muted-foreground space-y-2 px-5 py-4 text-[12.5px] leading-relaxed">
        <p>
          <span className="text-foreground font-medium">What Foray reads.</span> It asks Google
          for read-only access and uses it for one thing: finding employers&rsquo; replies to
          applications you sent. Every 15 minutes it searches the last two weeks of mail for
          the words such replies use, and reads each result&rsquo;s sender, subject line, date
          and the one-line preview Gmail shows. It never opens a message&rsquo;s body or
          attachments, and it cannot send, delete, label or change mail.
        </p>
        <p>
          <span className="text-foreground font-medium">What it keeps.</span> For a
          confirmation, a rejection or an interview invitation: the sender&rsquo;s name and
          domain, the subject line, the preview and the date. For anything else: only that it
          has been looked at. Mail that turns out to be about none of your applications is
          forgotten after 30 days. No email is shown to an AI model; fixed rules sort them. A
          rejection or an invitation only suggests moving a match, and you decide.
          Disconnecting deletes all of it and tells Google to forget the access.
        </p>
      </div>
    </div>
  );
}
