"use client";

import { Search, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useTransition, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

/**
 * Searches the user's contacts by name, company, position or school. The search is the page's
 * own read (`/contacts?q=…`): Django filters every contact, so a person past a list's cap is
 * found too, and the address can be reloaded or shared with oneself.
 */
export function ContactSearch({ query }: { query: string }) {
  const router = useRouter();
  const [value, setValue] = useState(query);
  const [pending, startTransition] = useTransition();
  // The address bar is the truth: when it changes (back, a link), the box follows it.
  const [shown, setShown] = useState(query);
  if (shown !== query) {
    setShown(query);
    setValue(query);
  }

  function go(next: string) {
    const trimmed = next.trim();
    startTransition(() => {
      router.push(trimmed ? `/contacts?q=${encodeURIComponent(trimmed)}` : "/contacts");
    });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    go(value);
  }

  return (
    <form className="flex max-w-md gap-2" onSubmit={submit} role="search">
      <div className="relative min-w-0 flex-1">
        <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
        <Input
          aria-label="Search contacts"
          className="h-9 pr-8 pl-8 text-[13.5px] md:text-[13.5px] [&::-webkit-search-cancel-button]:hidden"
          onChange={(event) => setValue(event.target.value)}
          placeholder="Search by name, company, title or school"
          type="search"
          value={value}
        />
        {(value || query) && (
          <button
            aria-label="Clear search"
            className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2 inline-flex size-5 -translate-y-1/2 items-center justify-center rounded"
            onClick={() => {
              setValue("");
              if (query) go("");
            }}
            type="button"
          >
            <X className="size-3.5" />
          </button>
        )}
      </div>
      <Button className="h-9" disabled={pending} type="submit" variant="outline">
        {pending && <Spinner />}
        Search
      </Button>
    </form>
  );
}
