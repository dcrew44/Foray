import { LiveProvider } from "@/components/live/live-provider";
import { AppShell } from "@/components/shell/app-shell";
import { Toaster } from "@/components/shell/toaster";
import { requireUser } from "@/lib/auth/session";

/**
 * Every signed-in page: the shell, the live channel to background work, and
 * toasts. Pages still call requireUser() themselves — a layout does not re-run
 * on every navigation, so it cannot be the only guard.
 */
export default async function AppLayout({ children }: LayoutProps<"/">) {
  const user = await requireUser();

  return (
    <LiveProvider>
      <AppShell user={{ name: user.name, email: user.email }}>{children}</AppShell>
      <Toaster />
    </LiveProvider>
  );
}
