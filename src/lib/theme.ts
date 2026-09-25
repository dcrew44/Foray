/**
 * Light, dark, or whatever the operating system says — and dark when nobody
 * has chosen.
 *
 * One module so the three places that care agree: the script that sets the
 * class before first paint, the appearance menu, and the toasts. The choice
 * lives in localStorage; the effective theme is the `dark` class on <html>.
 * Functions that touch the DOM are only ever called in the browser.
 */

export type ThemeChoice = "light" | "dark" | "system";

export const THEME_STORAGE_KEY = "foray-theme";
export const DEFAULT_THEME: ThemeChoice = "dark";

/** Fired on window whenever the effective theme may have changed. */
export const THEME_EVENT = "foray:theme";

/** The canvas colour per theme, for the browser's own chrome. */
export const THEME_COLORS = { light: "#ecefea", dark: "#0d1310" } as const;

function isChoice(value: unknown): value is ThemeChoice {
  return value === "light" || value === "dark" || value === "system";
}

export function readThemeChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isChoice(stored) ? stored : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export function prefersDark(choice: ThemeChoice): boolean {
  return (
    choice === "dark" ||
    (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches)
  );
}

/** Puts the page in the theme a choice resolves to, right now. */
export function applyEffectiveTheme(choice: ThemeChoice = readThemeChoice()) {
  const dark = prefersDark(choice);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? THEME_COLORS.dark : THEME_COLORS.light);
  window.dispatchEvent(new Event(THEME_EVENT));
}

/** Records the user's choice and applies it. */
export function chooseTheme(choice: ThemeChoice) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // Storage blocked: the choice still applies for this page view.
  }
  applyEffectiveTheme(choice);
}

/** Whether the page is dark right now, for components that need to know. */
export function isDarkNow(): boolean {
  return document.documentElement.classList.contains("dark");
}

/**
 * Runs before first paint (inlined into <head>), so nobody sees the wrong
 * theme flash. Built from the constants above so it cannot drift from them.
 */
export const THEME_SCRIPT = `(() => {
  try {
    const stored = localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
    const choice = stored === "light" || stored === "dark" || stored === "system" ? stored : ${JSON.stringify(DEFAULT_THEME)};
    const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    const root = document.documentElement;
    root.classList.toggle("dark", dark);
    root.style.colorScheme = dark ? "dark" : "light";
  } catch {}
})();`;
