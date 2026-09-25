"use client";

import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * "On this page" for the long pages — profile and settings. Wide screens only;
 * it follows the reader, marking the section they are in.
 */

export type PageNavItem = {
  id: string;
  label: string;
  /** A short trailing note, e.g. how many answers are still needed. */
  detail?: string;
  /** Nested under the item before it. */
  child?: boolean;
};

/** A section counts as current once its top passes this far down the window. */
const OFFSET = 140;

export function PageNav({ items, className }: { items: PageNavItem[]; className?: string }) {
  const [active, setActive] = useState<string | null>(items[0]?.id ?? null);

  useEffect(() => {
    let frame = 0;
    const measure = () => {
      frame = 0;
      let current = items[0]?.id ?? null;
      for (const item of items) {
        const element = document.getElementById(item.id);
        if (element && element.getBoundingClientRect().top <= OFFSET) current = item.id;
      }
      // At the very bottom the last section may never reach the offset.
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
        current = items.at(-1)?.id ?? current;
      }
      setActive(current);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(measure);
    };

    schedule();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [items]);

  // A parent reads as current while any of its children is.
  const activeIndex = items.findIndex((item) => item.id === active);
  let activeParent: string | null = null;
  for (let index = activeIndex; index >= 0; index--) {
    if (!items[index]!.child) {
      activeParent = items[index]!.id;
      break;
    }
  }

  return (
    <nav aria-label="On this page" className={cn("text-[13px]", className)}>
      <ul className="border-l">
        {items.map((item) => {
          const current = item.id === active;
          const inside = !item.child && item.id === activeParent && !current;
          return (
            <li key={item.id}>
              <a
                aria-current={current ? "location" : undefined}
                className={cn(
                  "-ml-px flex items-baseline justify-between gap-3 border-l-2 border-transparent py-1 pr-2 transition-colors",
                  item.child ? "pl-6" : "pl-3.5",
                  current
                    ? "border-primary text-foreground font-medium"
                    : inside
                      ? "text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                )}
                href={`#${item.id}`}
              >
                <span className="min-w-0 truncate">{item.label}</span>
                {item.detail && (
                  <span className="text-muted-foreground tabular shrink-0 text-[12px] font-normal">
                    {item.detail}
                  </span>
                )}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
