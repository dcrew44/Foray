"use client";

import { Toaster as Sonner } from "sonner";

/** Toasts in the app's own voice: quiet surface, pine for success, brick for errors. */
export function Toaster() {
  return (
    <Sonner
      closeButton
      duration={6000}
      position="bottom-right"
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
