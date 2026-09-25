"use client";

import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";
import { Monitor, Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";

import {
  chooseTheme,
  DEFAULT_THEME,
  readThemeChoice,
  THEME_EVENT,
  type ThemeChoice,
} from "@/lib/theme";
import { cn } from "@/lib/utils";

const OPTIONS: { value: ThemeChoice; label: string; detail: string; icon: typeof Moon }[] = [
  { value: "dark", label: "Dark", detail: "Easy on the eyes. The default.", icon: Moon },
  { value: "light", label: "Light", detail: "Paper-like, for bright rooms.", icon: Sun },
  {
    value: "system",
    label: "Match system",
    detail: "Follows your computer, and switches when it does.",
    icon: Monitor,
  },
];

function subscribe(callback: () => void) {
  window.addEventListener(THEME_EVENT, callback);
  return () => window.removeEventListener(THEME_EVENT, callback);
}

/** The theme choice, applied the moment it is picked. The same one the account menu sets. */
export function AppearanceForm() {
  const theme = useSyncExternalStore(subscribe, readThemeChoice, () => DEFAULT_THEME);

  return (
    <RadioGroup
      aria-label="Theme"
      className="grid gap-2.5 sm:grid-cols-3"
      onValueChange={(value) => chooseTheme(value as ThemeChoice)}
      value={theme}
    >
      {OPTIONS.map((option) => {
        const Icon = option.icon;
        return (
          <Radio.Root
            className={cn(
              "group/theme flex cursor-default gap-3 rounded-lg border px-3.5 py-3 text-left transition-colors outline-none",
              "hover:bg-muted/50 focus-visible:ring-ring/50 focus-visible:ring-3",
              "data-checked:border-primary/60 data-checked:bg-accent/60",
            )}
            key={option.value}
            value={option.value}
          >
            <Icon className="text-muted-foreground group-data-checked/theme:text-primary mt-0.5 size-4 shrink-0" />
            <span className="min-w-0">
              <span className="block text-[13.5px] font-medium">{option.label}</span>
              <span className="text-muted-foreground mt-0.5 block text-[12.5px] leading-snug">
                {option.detail}
              </span>
            </span>
          </Radio.Root>
        );
      })}
    </RadioGroup>
  );
}
