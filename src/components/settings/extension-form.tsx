"use client";

import { Check, Copy, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useSyncExternalStore, useTransition } from "react";
import { toast } from "sonner";

import { FormRow, SaveBar, useUnsavedChanges } from "@/components/profile/form-controls";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ExtensionToken, SavedSettings } from "@/lib/api/types";
import { timeAgo } from "@/lib/format";

/**
 * The browser extension that fills application forms in the user's own
 * browser (#22): the tokens it signs in with, and how far Foray goes with it.
 *
 * A token is shown once, the moment it is made; Foray keeps only its hash. The
 * two limits are Foray's restraint, counted on the server: past them it stops
 * filling, and the user can still apply by hand. Pressing submit is the user's
 * unless they turn that on here, and then only inside those limits.
 */

type SettingsPatch = components["schemas"]["SettingsPatch"];
/**
 * `autoSubmit` (#40) is in Django's settings but not yet in the types
 * generated on this branch, so it is read and sent through this one widening.
 */
type WithAutoSubmit<T> = T & { autoSubmit?: boolean };

const PER_EMPLOYER = { min: 1, max: 10 };
const PER_DAY = { min: 1, max: 100 };
const MAX_TOKENS = 5;

/** A whole number inside a range, or null while the field holds anything else. */
function within(text: string, range: { min: number; max: number }): number | null {
  if (!/^\d{1,3}$/.test(text.trim())) return null;
  const value = Number(text);
  return value >= range.min && value <= range.max ? value : null;
}

const subscribe = () => () => {};
/** This page's own address is the one the extension needs; nothing on the server. */
const pageOrigin = () => window.location.origin;

export function ExtensionForm({
  initial,
  tokens,
}: {
  initial: SavedSettings;
  tokens: ExtensionToken[];
}) {
  const router = useRouter();
  const baseline = {
    perEmployer: String(initial.applyPerEmployer),
    perDay: String(initial.applyDailyCap),
    autoSubmit: (initial as WithAutoSubmit<SavedSettings>).autoSubmit ?? false,
  };
  const [perEmployer, setPerEmployer] = useState(baseline.perEmployer);
  const [perDay, setPerDay] = useState(baseline.perDay);
  const [autoSubmit, setAutoSubmit] = useState(baseline.autoSubmit);
  const [label, setLabel] = useState("");
  // A token just made: the only time its secret exists on this page.
  const [made, setMade] = useState<string | null>(null);
  const [copied, setCopied] = useState<"token" | "address" | null>(null);
  const [making, setMaking] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const origin = useSyncExternalStore(subscribe, pageOrigin, () => "");

  const employer = within(perEmployer, PER_EMPLOYER);
  const day = within(perDay, PER_DAY);
  const dirty =
    perEmployer !== baseline.perEmployer ||
    perDay !== baseline.perDay ||
    autoSubmit !== baseline.autoSubmit;

  function save() {
    if (!dirty || pending) return;
    if (employer === null || day === null) {
      toast.error("The limits need whole numbers in their ranges.");
      return;
    }
    startTransition(async () => {
      const body: WithAutoSubmit<SettingsPatch> = {
        applyPerEmployer: employer,
        applyDailyCap: day,
        // Sent only when it changed, so the limits save on their own.
        ...(autoSubmit !== baseline.autoSubmit ? { autoSubmit } : {}),
      };
      const result = await call(api.PATCH("/api/settings", { body: body as SettingsPatch }));
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast.success(result.message);
      router.refresh();
    });
  }

  function discard() {
    setPerEmployer(baseline.perEmployer);
    setPerDay(baseline.perDay);
    setAutoSubmit(baseline.autoSubmit);
  }

  useUnsavedChanges(dirty, save);

  async function copy(text: string, what: "token" | "address") {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied((current) => (current === what ? null : current)), 2000);
    } catch {
      toast.error("Could not copy. Select the text and copy it yourself.");
    }
  }

  async function makeToken() {
    if (making) return;
    setMaking(true);
    const result = await call(api.POST("/api/extension-tokens", { body: { label: label.trim() } }));
    setMaking(false);
    if (!result.ok) {
      toast.error(result.message);
      return;
    }
    setMade(result.token);
    setLabel("");
    router.refresh();
  }

  async function removeToken(id: string) {
    setRemoving(id);
    const result = await call(
      api.DELETE("/api/extension-tokens/{id}", { params: { path: { id } } }),
    );
    setRemoving(null);
    if (!result.ok) {
      toast.error(result.message);
      return;
    }
    toast.success(result.message);
    router.refresh();
  }

  return (
    <div className="space-y-4">
      <fieldset className="min-w-0 divide-y rounded-xl border">
        <div className="space-y-3 px-5 py-4">
          <div className="space-y-1">
            <p className="text-[13.5px] font-medium">Connect the extension</p>
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              Load it in Chrome from the <code className="text-foreground">extension</code>{" "}
              folder (<code className="text-foreground">docs/extension.md</code> has the steps),
              open its settings, and give it this address and a token.
            </p>
          </div>
          {origin && (
            <div className="flex flex-wrap items-center gap-2">
              <code className="bg-muted rounded-md px-2.5 py-1.5 text-[13px]">{origin}</code>
              <Button onClick={() => void copy(origin, "address")} size="sm" variant="outline">
                {copied === "address" ? <Check /> : <Copy />}
                {copied === "address" ? "Copied" : "Copy address"}
              </Button>
            </div>
          )}
        </div>

        <div className="space-y-3 px-5 py-4">
          <div className="space-y-1">
            <p className="text-[13.5px] font-medium" id="extension-tokens-label">
              Tokens
            </p>
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              A token signs one browser&rsquo;s extension in. It can read your prepared
              applications and report what it filled, and nothing else. Remove one and the
              extension using it is signed out.
            </p>
          </div>

          {tokens.length > 0 && (
            <ul aria-labelledby="extension-tokens-label" className="divide-y rounded-lg border">
              {tokens.map((token) => (
                <li
                  className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3.5 py-2.5"
                  key={token.id}
                >
                  <div className="min-w-0">
                    <p className="truncate text-[13.5px] font-medium">
                      {token.label || "Unnamed token"}
                    </p>
                    <p className="text-muted-foreground text-[12.5px]">
                      Made {timeAgo(token.createdAt)}.{" "}
                      {token.lastUsedAt ? `Last used ${timeAgo(token.lastUsedAt)}.` : "Not used yet."}
                    </p>
                  </div>
                  <Button
                    aria-label={`Remove the token ${token.label || "without a name"}`}
                    disabled={removing === token.id}
                    onClick={() => void removeToken(token.id)}
                    size="sm"
                    variant="ghost"
                  >
                    {removing === token.id && <Spinner />}
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}

          {made ? (
            <div className="bg-signal-soft text-signal-foreground space-y-2.5 rounded-lg px-3.5 py-3" role="status">
              <p className="text-[13.5px] font-medium">
                Copy this token into the extension now. It will not be shown again.
              </p>
              <code className="bg-background text-foreground block rounded-md px-2.5 py-2 text-[12.5px] break-all select-all">
                {made}
              </code>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => void copy(made, "token")} size="sm">
                  {copied === "token" ? <Check /> : <Copy />}
                  {copied === "token" ? "Copied" : "Copy token"}
                </Button>
                <Button onClick={() => setMade(null)} size="sm" variant="ghost">
                  Done
                </Button>
              </div>
            </div>
          ) : tokens.length >= MAX_TOKENS ? (
            <p className="text-muted-foreground text-[12.5px]">
              That is as many tokens as Foray keeps. Remove one you no longer use to make another.
            </p>
          ) : (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void makeToken();
              }}
            >
              <Input
                aria-label="A name for the new token"
                autoComplete="off"
                className="h-9 w-56 text-[13.5px] md:text-[13.5px]"
                maxLength={80}
                onChange={(event) => setLabel(event.target.value)}
                placeholder="Name it, for example Laptop"
                value={label}
              />
              <Button disabled={making} type="submit" variant="outline">
                {making ? <Spinner /> : <Plus />}
                Make a token
              </Button>
            </form>
          )}
        </div>

        <div className="grid gap-x-8 gap-y-3 px-5 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <div className="min-w-0 space-y-1">
            <p className="text-[13.5px] font-medium" id="auto-submit-label">
              Let the extension press submit itself
            </p>
            <p className="text-muted-foreground text-[12.5px] leading-snug">
              Only for matches in the automated lane, when every answer is one you saved, within
              the limits below. Anything else still waits for you.
            </p>
          </div>
          <Switch
            aria-labelledby="auto-submit-label"
            checked={autoSubmit}
            onCheckedChange={setAutoSubmit}
          />
        </div>

        <LimitRow
          hint="Foray fills at most this many applications to one employer in 60 days. Past that it stops; you can still apply yourself."
          id="apply-per-employer"
          label="Applications to one employer"
          onChange={setPerEmployer}
          range={PER_EMPLOYER}
          valid={employer !== null}
          value={perEmployer}
        />

        <LimitRow
          hint="The most applications Foray fills in 24 hours, to anyone."
          id="apply-daily-cap"
          label="Applications a day"
          onChange={setPerDay}
          range={PER_DAY}
          valid={day !== null}
          value={perDay}
        />
      </fieldset>

      <SaveBar
        message="Unsaved changes to the browser extension."
        onDiscard={discard}
        onSave={save}
        pending={pending}
        saveLabel="Save extension settings"
        visible={dirty}
      />
    </div>
  );
}

function LimitRow({
  id,
  label,
  hint,
  value,
  valid,
  range,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  value: string;
  valid: boolean;
  range: { min: number; max: number };
  onChange: (value: string) => void;
}) {
  return (
    <FormRow
      hint={hint}
      id={id}
      label={label}
      note={
        !valid && (
          <p className="text-destructive text-[12.5px] leading-snug">
            A whole number from {range.min} to {range.max}.
          </p>
        )
      }
    >
      <Input
        aria-invalid={!valid}
        className="tabular h-9 w-24 text-[13.5px] md:text-[13.5px]"
        id={id}
        inputMode="numeric"
        max={range.max}
        min={range.min}
        onChange={(event) => onChange(event.target.value)}
        type="number"
        value={value}
      />
    </FormRow>
  );
}
