"use client";

import { useSyncExternalStore } from "react";
import { Toaster as Sonner } from "sonner";

import { isDarkNow, THEME_EVENT } from "@/lib/theme";

function subscribe(callback: () => void) {
  window.addEventListener(THEME_EVENT, callback);
  return () => window.removeEventListener(THEME_EVENT, callback);
}

/** Toasts in the app's own voice: quiet surface, pine for success, brick for errors. */
export function Toaster() {
  // Follows the page's theme rather than the operating system's, so a light
  // choice on a dark machine (or the dark default on a light one) matches.
  const dark = useSyncExternalStore(subscribe, isDarkNow, () => true);

  return (
    <Sonner
      closeButton
      duration={6000}
      position="bottom-right"
      theme={dark ? "dark" : "light"}
      toastOptions={{
        classNames: {
          toast:
            "!bg-popover !text-popover-foreground !border-border !rounded-xl !shadow-lg !font-sans !text-[13.5px]",
          description: "!text-muted-foreground",
          success: "[&_[data-icon]]:!text-success",
          error: "[&_[data-icon]]:!text-destructive",
          closeButton: "!bg-popover !border-border !text-muted-foreground",
        },
      }}
    />
  );
}
