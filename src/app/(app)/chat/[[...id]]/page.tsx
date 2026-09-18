import { requireUser } from "@/lib/auth/session";

/**
 * Placeholder until Step 7 replaces it with the AI Elements conversation.
 * It exists now so the auth redirect has something real to protect.
 */
export default async function ChatPage() {
  const user = await requireUser();

  return (
    <main className="mx-auto flex min-h-svh max-w-2xl flex-col justify-center gap-2 p-8">
      <h1 className="text-2xl font-semibold">Chat</h1>
      <p className="text-muted-foreground text-sm">
        Signed in as {user.email}. The conversation lands here in Step 7.
      </p>
    </main>
  );
}
