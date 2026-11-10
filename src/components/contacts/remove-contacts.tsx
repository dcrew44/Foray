"use client";

import { X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import { plural } from "@/lib/format";

/** Removes one person from the user's contacts. Importing their file again brings them back. */
export function RemoveContactButton({ id, name }: { id: string; name: string }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  return (
    <Button
      aria-label={`Remove ${name}`}
      className="text-muted-foreground shrink-0"
      disabled={pending}
      onClick={() =>
        startTransition(async () => {
          const result = await call(
            api.DELETE("/api/contacts/{id}", { params: { path: { id } } }),
          );
          if (!result.ok) {
            toast.error(result.message);
            return;
          }
          toast(`Removed ${name}`);
          router.refresh();
        })
      }
      size="icon-xs"
      title={`Remove ${name}`}
      variant="ghost"
    >
      {pending ? <Spinner /> : <X />}
    </Button>
  );
}

/**
 * Removes every contact, in two presses: the first asks, the second does it. The files they
 * came from are the user's own, so nothing is lost that an import cannot bring back.
 */
export function RemoveAllContacts({ total }: { total: number }) {
  const router = useRouter();
  const [asking, setAsking] = useState(false);
  const [pending, startTransition] = useTransition();

  function remove() {
    startTransition(async () => {
      const result = await call(api.DELETE("/api/contacts"));
      setAsking(false);
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      toast(result.message);
      // Back to the bare page: a search of contacts that are gone would show nothing.
      router.push("/contacts");
      router.refresh();
    });
  }

  if (!asking) {
    return (
      <Button onClick={() => setAsking(true)} size="sm" variant="outline">
        Remove all contacts
      </Button>
    );
  }
  return (
    <div
      aria-label="Remove all contacts"
      className="flex flex-wrap items-center gap-2 text-[13px]"
      role="group"
    >
      <span>
        Remove all {plural(total, "contact")} from Foray? Your files are not touched, and you
        can import them again.
      </span>
      <Button disabled={pending} onClick={remove} size="sm" variant="destructive">
        {pending && <Spinner />}
        Remove them
      </Button>
      <Button disabled={pending} onClick={() => setAsking(false)} size="sm" variant="ghost">
        Keep them
      </Button>
    </div>
  );
}
