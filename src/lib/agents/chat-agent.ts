import { ToolLoopAgent, isStepCount, type InferAgentUIMessage } from "ai";

import { model } from "@/lib/llm/models";
import { getProfileSummary } from "./tools/get-profile-summary";
import { SYSTEM_PROMPT } from "./system-prompt";

/**
 * The chat agent, built per request around one user.
 *
 * The step cap matters: a chat turn runs inside a Next request, so a loop that
 * will not terminate has to be bounded rather than merely discouraged.
 */
export function chatAgent(userId: string) {
  return new ToolLoopAgent({
    model: model("chat"),
    instructions: SYSTEM_PROMPT,
    tools: {
      get_profile_summary: getProfileSummary(userId),
    },
    stopWhen: isStepCount(8),
  });
}

/**
 * The UI message type for *this* agent, with its tool names and input/output
 * types baked in. The client infers tool parts from it, so a renamed tool is a
 * type error rather than a silently unrendered part.
 */
export type ForayUIMessage = InferAgentUIMessage<ReturnType<typeof chatAgent>>;
