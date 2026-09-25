"use client";

import { Ellipsis, Trash } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type Conversation = { id: string; title: string; updatedAt: string };

/** Fired by the chat when a conversation is created or renamed. */
export const CONVERSATIONS_CHANGED = "foray:conversations-changed";

export function announceConversationsChanged() {
  window.dispatchEvent(new Event(CONVERSATIONS_CHANGED));
}

/**
 * Recent conversations, fetched on the client so the list updates the moment
 * a new chat's first reply lands — the layout that hosts the sidebar is not
 * re-rendered by navigating between chats.
 */
export function RecentChats() {
  const pathname = usePathname();
  const router = useRouter();
  const [items, setItems] = useState<Conversation[] | null>(null);

  const load = useCallback(async () => {
    const response = await fetch("/api/conversations", { cache: "no-store" });
    if (!response.ok) return;
    const body = (await response.json()) as { conversations: Conversation[] };
    setItems(body.conversations);
  }, []);

  useEffect(() => {
    // Deferred so the fetch is a subscription, not a render-phase state update.
    const initial = setTimeout(() => void load(), 0);
    const onChange = () => void load();
    window.addEventListener(CONVERSATIONS_CHANGED, onChange);
    return () => {
      clearTimeout(initial);
      window.removeEventListener(CONVERSATIONS_CHANGED, onChange);
    };
  }, [load]);

  async function remove(id: string) {
    setItems((previous) => previous?.filter((item) => item.id !== id) ?? null);
    await fetch(`/api/conversations/${id}`, { method: "DELETE" });
    if (pathname === `/chat/${id}`) router.push("/chat");
  }

  if (!items || items.length === 0) return null;

  return (
    <div className="mt-6 flex min-h-0 flex-1 flex-col">
      <p className="text-muted-foreground px-2.5 pb-1.5 text-[12px] font-medium">
        Recent chats
      </p>
      <ul className="scrollbar-thin min-h-0 flex-1 space-y-px overflow-y-auto">
        {items.slice(0, 20).map((item) => {
          const active = pathname === `/chat/${item.id}`;
          return (
            <li className="group/chat relative" key={item.id}>
              <Link
                className={cn(
                  "text-sidebar-foreground block truncate rounded-md py-1.5 pr-8 pl-2.5 text-[13px] transition-colors",
                  active
                    ? "bg-sidebar-accent text-foreground font-medium"
                    : "hover:bg-sidebar-accent",
                )}
                href={`/chat/${item.id}`}
                title={item.title}
              >
                {item.title}
              </Link>
              <DropdownMenu>
                <DropdownMenuTrigger
                  aria-label={`Options for ${item.title}`}
                  className="text-muted-foreground hover:text-foreground data-popup-open:opacity-100 absolute top-1/2 right-1 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-md opacity-0 group-hover/chat:opacity-100 focus-visible:opacity-100"
                >
                  <Ellipsis className="size-4" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-40">
                  <DropdownMenuItem
                    onClick={() => void remove(item.id)}
                    variant="destructive"
                  >
                    <Trash />
                    Delete chat
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
