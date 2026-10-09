import type { StageId } from "@/lib/format";

/**
 * The board's stages in order. No directive: Home (a server component) and the board, the list
 * and the stage picker (client components) all read them, so they cannot live in a
 * `"use client"` module.
 */

/** Still in play: the board's columns. */
export const OPEN_STAGES: StageId[] = ["found", "shortlisted", "applied", "interviewing", "offer"];

/** Out of play: hidden on the board unless asked for. */
export const CLOSED_STAGES: StageId[] = ["rejected", "archived"];

export const ALL_STAGES: StageId[] = [...OPEN_STAGES, ...CLOSED_STAGES];
