"use client";

import { MessageSquarePlus } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";

import { startChatWith } from "./ask-box";

/** Opens a chat that starts the preferences interview straight away. */
export function OnboardingChat() {
  const router = useRouter();
  return (
    <Button
      onClick={() =>
        startChatWith(router, "Let's set up my job search. Ask me what you need to know.")
      }
    >
      <MessageSquarePlus />
      Answer in chat
    </Button>
  );
}
