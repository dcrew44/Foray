import { LiveProvider } from "@/components/live/live-provider";
import { AppShell } from "@/components/shell/app-shell";
import { Toaster } from "@/components/shell/toaster";
import { getMe } from "@/lib/api/server";

/**
 * Every signed-in page: the shell, the live channel to background work, and toasts.
 *
 * `getMe()` reads `GET /api/me` as the visitor; a 401 sends them to sign in and back. A layout
 * does not re-run on every navigation, so it is not the only guard: each page's own Django read
 * is authenticated too (design §6.1).
 */
export default async function AppLayout({ children }: LayoutProps<"/">) {
  const user = await getMe();

  return (
    <LiveProvider>
      <AppShell user={{ name: user.name, email: user.email }}>{children}</AppShell>
      <Toaster />
    </LiveProvider>
  );
}
