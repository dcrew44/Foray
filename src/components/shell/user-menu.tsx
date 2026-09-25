"use client";

import { ChevronDown, LogOut, Monitor, Moon, Settings2, Sun } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSyncExternalStore } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { signOut } from "@/lib/auth/client";
import { initials } from "@/lib/format";

import type { ShellUser } from "./app-shell";

type Theme = "light" | "dark" | "system";

const THEME_EVENT = "foray:theme";

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem("foray-theme");
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme: Theme) {
  try {
    if (theme === "system") localStorage.removeItem("foray-theme");
    else localStorage.setItem("foray-theme", theme);
  } catch {
    // Storage blocked: the choice still applies for this page view.
  }
  const dark =
    theme === "dark" ||
    (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  window.dispatchEvent(new Event(THEME_EVENT));
}

function subscribe(callback: () => void) {
  window.addEventListener(THEME_EVENT, callback);
  return () => window.removeEventListener(THEME_EVENT, callback);
}

export function UserMenu({ user }: { user: ShellUser }) {
  const router = useRouter();
  const theme = useSyncExternalStore(subscribe, readTheme, () => "system" as Theme);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="hover:bg-sidebar-accent data-popup-open:bg-sidebar-accent flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50">
        <span className="bg-primary/10 text-primary text-display inline-flex size-8 shrink-0 items-center justify-center rounded-full text-[12px]">
          {initials(user.name || user.email)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium">
            {user.name || user.email}
          </span>
          <span className="text-muted-foreground block truncate text-[12px]">
            {user.email}
          </span>
        </span>
        <ChevronDown className="text-muted-foreground size-4 shrink-0" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56" side="top">
        <DropdownMenuLabel>Appearance</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          onValueChange={(value) => applyTheme(value as Theme)}
          value={theme}
        >
          <DropdownMenuRadioItem value="light">
            <Sun />
            Light
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <Moon />
            Dark
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">
            <Monitor />
            Match system
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem render={<Link href="/settings" />}>
          <Settings2 />
          Settings
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={async () => {
            await signOut();
            router.push("/sign-in");
            router.refresh();
          }}
        >
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
