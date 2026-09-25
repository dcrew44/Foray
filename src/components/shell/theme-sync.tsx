"use client";

import { useEffect } from "react";

import { applyEffectiveTheme, readThemeChoice, THEME_STORAGE_KEY } from "@/lib/theme";

/**
 * Keeps the page's theme current after first paint: follows the operating
 * system live for anyone on "Match system", and picks up a change made in
 * another tab. Renders nothing.
 */
export function ThemeSync() {
  useEffect(() => {
    // The server renders dark; this corrects the colour-scheme hints and the
    // browser chrome once, for anyone whose choice resolves to light.
    applyEffectiveTheme();

    const media = matchMedia("(prefers-color-scheme: dark)");
    const onSystemChange = () => {
      if (readThemeChoice() === "system") applyEffectiveTheme("system");
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY) applyEffectiveTheme();
    };

    media.addEventListener("change", onSystemChange);
    window.addEventListener("storage", onStorage);
    return () => {
      media.removeEventListener("change", onSystemChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  return null;
}
