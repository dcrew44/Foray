"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { signOut } from "@/lib/api/auth";

export function SignOutButton() {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  return (
    <Button
      disabled={pending}
      onClick={async () => {
        setPending(true);
        const result = await signOut();
        if (!result.ok) {
          setPending(false);
          toast.error("Signing out did not work. Reload the page and try again.");
          return;
        }
        router.push("/sign-in");
        router.refresh();
      }}
      variant="outline"
    >
      {pending ? <Spinner /> : <LogOut />}
      {pending ? "Signing out…" : "Sign out"}
    </Button>
  );
}
