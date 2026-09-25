"use client";

import {
  Activity,
  Building,
  House,
  Menu,
  MessageSquarePlus,
  Settings2,
  SquareKanban,
  UserRound,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";

import { Blaze, SignalDot, Wordmark } from "@/components/foray/primitives";
import { isActiveRun, useLive } from "@/components/live/live-provider";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

import { RecentChats } from "./recent-chats";
import { UserMenu } from "./user-menu";

/**
 * The frame around every signed-in page: a quiet sidebar on the canvas, and the
 * page itself on a raised white sheet. On small screens the sidebar becomes a
 * sheet behind a menu button.
 */

export type ShellUser = { name: string; email: string };

type NavItem = {
  href: string;
  label: string;
  icon: typeof House;
  match: (path: string) => boolean;
};

const PRIMARY: NavItem[] = [
  { href: "/home", label: "Home", icon: House, match: (p) => p === "/home" },
  {
    href: "/matches",
    label: "Matches",
    icon: SquareKanban,
    match: (p) => p.startsWith("/matches"),
  },
  {
    href: "/activity",
    label: "Activity",
    icon: Activity,
    match: (p) => p.startsWith("/activity"),
  },
];

const SECONDARY: NavItem[] = [
  {
    href: "/profile",
    label: "Profile",
    icon: UserRound,
    match: (p) => p.startsWith("/profile"),
  },
  {
    href: "/companies",
    label: "Companies",
    icon: Building,
    match: (p) => p.startsWith("/companies"),
  },
  {
    href: "/settings",
    label: "Settings",
    icon: Settings2,
    match: (p) => p.startsWith("/settings"),
  },
];

export function AppShell({
  user,
  children,
}: {
  user: ShellUser;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const live = useLive();
  const working = Object.values(live.runs).some(isActiveRun);

  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 hidden h-svh w-[248px] shrink-0 flex-col lg:flex">
        <SidebarContent user={user} />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col lg:py-2 lg:pr-2">
        <div className="bg-canvas/90 sticky top-0 z-30 flex h-14 items-center gap-3 border-b px-4 backdrop-blur lg:hidden">
          <button
            aria-label="Open navigation"
            className="hover:bg-sidebar-accent -ml-1.5 inline-flex size-9 items-center justify-center rounded-lg"
            onClick={() => setOpen(true)}
            type="button"
          >
            <Menu className="size-5" />
          </button>
          <Link href="/home">
            <Wordmark live={working} />
          </Link>
          <Link
            aria-label="New chat"
            className="hover:bg-sidebar-accent ml-auto inline-flex size-9 items-center justify-center rounded-lg"
            href="/chat"
          >
            <MessageSquarePlus className="size-5" />
          </Link>
        </div>

        <main className="bg-background flex min-h-[calc(100svh-3.5rem)] flex-1 flex-col lg:min-h-[calc(100svh-1rem)] lg:rounded-xl lg:border lg:shadow-[0_1px_2px_rgb(23_34_29/0.04)]">
          {children}
        </main>
      </div>

      <Sheet onOpenChange={setOpen} open={open}>
        <SheetContent
          className="bg-canvas w-[280px] gap-0 p-0 sm:max-w-[280px]"
          side="left"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <div className="flex h-full flex-col" onClick={(e) => {
            // Close after following a link, not after opening a menu.
            if ((e.target as HTMLElement).closest("a")) setOpen(false);
          }}>
            <SidebarContent user={user} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}

function SidebarContent({ user }: { user: ShellUser }) {
  const pathname = usePathname();
  const live = useLive();

  const working = Object.values(live.runs).some(isActiveRun);
  const waiting = live.approvals.length;

  return (
    <div className="flex h-full flex-col px-3 pt-4 pb-3">
      <Link
        className="mx-2 mb-5 inline-flex w-fit items-center rounded-md outline-offset-4"
        href="/home"
      >
        <Wordmark live={working} />
      </Link>

      <Link
        className={cn(
          "bg-primary text-primary-foreground mx-1 mb-4 inline-flex h-9 items-center gap-2 rounded-lg px-3 text-[13.5px] font-medium transition-colors hover:bg-primary/90",
          pathname === "/chat" && "ring-primary/30 ring-2 ring-offset-2 ring-offset-canvas",
        )}
        href="/chat"
      >
        <MessageSquarePlus className="size-4" />
        Ask Foray
      </Link>

      <nav aria-label="Primary" className="space-y-0.5">
        {PRIMARY.map((item) => (
          <NavLink
            active={item.match(pathname)}
            item={item}
            key={item.href}
            trailing={
              item.href === "/home" && waiting > 0 ? (
                <span className="bg-signal text-signal-foreground tabular inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 text-[11px] font-semibold">
                  {waiting}
                </span>
              ) : item.href === "/activity" && working ? (
                <SignalDot pulse />
              ) : null
            }
          />
        ))}
      </nav>

      <nav aria-label="Workspace" className="mt-4 space-y-0.5">
        {SECONDARY.map((item) => (
          <NavLink active={item.match(pathname)} item={item} key={item.href} />
        ))}
      </nav>

      <RecentChats />

      <div className="mt-auto space-y-2 pt-3">
        <WorkerLine />
        <UserMenu user={user} />
      </div>
    </div>
  );
}

function NavLink({
  item,
  active,
  trailing,
}: {
  item: NavItem;
  active: boolean;
  trailing?: ReactNode;
}) {
  const Icon = item.icon;
  return (
    <Link
      aria-current={active ? "page" : undefined}
      className={cn(
        "text-sidebar-foreground flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] transition-colors",
        active
          ? "bg-background text-foreground font-medium shadow-[0_1px_2px_rgb(23_34_29/0.06)] ring-1 ring-border"
          : "hover:bg-sidebar-accent",
      )}
      href={item.href}
    >
      <Icon className={cn("size-4", active ? "text-primary" : "opacity-70")} />
      <span className="flex-1">{item.label}</span>
      {trailing}
    </Link>
  );
}

/**
 * Whether the agents can actually do anything. A stopped worker is the most
 * common reason a search "does nothing", so it is said plainly here.
 */
function WorkerLine() {
  const { worker, ready, runs } = useLive();
  if (!ready || !worker) return null;

  const active = Object.values(runs).filter(isActiveRun);
  const working = active.filter((run) => run.status !== "awaiting_approval").length;
  const waiting = active.length - working;

  if (!worker.online) {
    return (
      <div className="border-destructive/25 bg-destructive/5 mx-1 rounded-lg border px-3 py-2 text-[12.5px] leading-snug">
        <div className="text-destructive flex items-center gap-2 font-medium">
          <SignalDot tone="error" />
          Worker offline
        </div>
        <p className="text-muted-foreground mt-0.5">
          Searches will wait until it runs. Start it with{" "}
          <code className="text-foreground">pnpm dev:worker</code>.
        </p>
      </div>
    );
  }

  return (
    <div className="text-muted-foreground flex items-center gap-2 px-3 text-[12.5px]">
      {working > 0 ? (
        <>
          <Blaze className="h-3 w-2 rounded-[2px]" live />
          <span className="ml-0.5">
            {working === 1 ? "1 agent working" : `${working} agents working`}
          </span>
        </>
      ) : waiting > 0 ? (
        <>
          <SignalDot />
          {waiting === 1 ? "1 run waiting on you" : `${waiting} runs waiting on you`}
        </>
      ) : (
        <>
          <SignalDot tone="ok" />
          Agents ready
        </>
      )}
    </div>
  );
}
