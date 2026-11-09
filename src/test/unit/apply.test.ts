import { describe, expect, it } from "vitest";

import {
  afterPosted,
  answerText,
  formName,
  missingAnswers,
  plannedAnswers,
  sourceLabel,
  type ApplyAnswer,
} from "@/lib/apply";

function answer(over: Partial<ApplyAnswer>): ApplyAnswer {
  return {
    key: "k",
    label: "Label",
    type: "text",
    required: true,
    options: [],
    kind: "custom",
    value: null,
    source: "missing",
    ...over,
  };
}

describe("prepared applications", () => {
  const answers = [
    answer({ key: "first_name", value: "Avery", source: "profile" }),
    answer({ key: "resume", value: "resume", source: "file", type: "file" }),
    answer({ key: "why", label: "Why us?" }),
    answer({ key: "preferred", required: false, source: "skipped" }),
    answer({ key: "veteran", value: "I don't wish to answer", source: "declined", required: false }),
  ];

  it("names only the required questions with no answer as missing", () => {
    expect(missingAnswers(answers).map((item) => item.key)).toEqual(["why"]);
  });

  it("lists what will be filled, in the form's order", () => {
    expect(plannedAnswers(answers).map((item) => item.key)).toEqual([
      "first_name",
      "resume",
      "veteran",
    ]);
  });

  it("says a file is attached instead of reading it out", () => {
    expect(answerText(answers[1])).toBe("Attached");
    expect(answerText(answers[0])).toBe("Avery");
    expect(answerText(answers[2])).toBe("");
  });

  it("words where an answer came from", () => {
    expect(sourceLabel("saved")).toBe("Your saved answer");
    expect(sourceLabel("declined")).toMatch(/Declined/);
  });

  it("names the form's board, or the employer's site", () => {
    expect(formName("greenhouse")).toBe("Greenhouse");
    expect(formName("")).toBe("the employer's site");
  });

  it("says how long after posting an application went out", () => {
    expect(afterPosted(null)).toBeNull();
    expect(afterPosted(3600)).toBe("the same day it was posted");
    expect(afterPosted(86400)).toBe("1 day after it was posted");
    expect(afterPosted(12 * 86400)).toBe("12 days after it was posted");
  });
});
