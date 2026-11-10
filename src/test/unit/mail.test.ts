import { describe, expect, it } from "vitest";

import {
  confirmationLine,
  foundSummary,
  suggestionQuestion,
  waitingLabel,
  type MailFound,
  type MailSuggestion,
} from "@/lib/mail";

const NOW = Date.parse("2026-11-09T12:00:00Z");

const rejection: MailSuggestion = {
  id: "0195f3a0-0000-7000-8000-000000000001",
  kind: "rejection",
  stage: "rejected",
  sender: "Northwind Robotics Recruiting",
  receivedAt: "2026-11-07T12:00:00Z",
  subject: "Your application to Northwind Robotics",
};

const found = (values: Partial<MailFound>): MailFound => ({
  confirmations: 0,
  rejections: 0,
  interviews: 0,
  matched: 0,
  waiting: 0,
  ...values,
});

describe("suggestionQuestion", () => {
  it("asks before anything moves, naming the sender, the subject and the stage", () => {
    expect(suggestionQuestion(rejection, NOW)).toBe(
      "An email from Northwind Robotics Recruiting, 2d ago, looks like a rejection: " +
        "“Your application to Northwind Robotics”. Move this match to Rejected?",
    );
  });

  it("calls an invitation an invitation and suggests Interviewing", () => {
    const invitation: MailSuggestion = {
      ...rejection,
      kind: "interview",
      stage: "interviewing",
      subject: "  ",
    };
    expect(suggestionQuestion(invitation, NOW)).toBe(
      "An email from Northwind Robotics Recruiting, 2d ago, looks like an interview invitation. " +
        "Move this match to Interviewing?",
    );
  });
});

describe("confirmationLine", () => {
  it("says when the employer's confirmation came and what it was called", () => {
    expect(confirmationLine("2026-11-06T12:00:00Z", "Thank you for applying", NOW)).toBe(
      "The employer confirmed your application arrived, 3d ago: “Thank you for applying”.",
    );
    expect(confirmationLine("2026-11-09T09:00:00Z", null, NOW)).toBe(
      "The employer confirmed your application arrived, 3h ago.",
    );
  });
});

describe("foundSummary", () => {
  it("is nothing until something that looks like an answer was found", () => {
    expect(foundSummary(found({}))).toBeNull();
  });

  it("lists what was found and how much of it was matched", () => {
    expect(foundSummary(found({ confirmations: 1, matched: 1 }))).toBe(
      "1 confirmation, matched to one of your applications.",
    );
    expect(foundSummary(found({ rejections: 2 }))).toBe(
      "2 rejections, none matched to an application yet.",
    );
    expect(foundSummary(found({ confirmations: 2, interviews: 1, matched: 3 }))).toBe(
      "2 confirmations and 1 interview invitation, all matched to your applications.",
    );
    expect(
      foundSummary(found({ confirmations: 3, rejections: 1, interviews: 2, matched: 4 })),
    ).toBe(
      "3 confirmations, 1 rejection and 2 interview invitations, 4 of them matched to your applications.",
    );
  });
});

describe("waitingLabel", () => {
  it("names the email and the role it is about", () => {
    expect(
      waitingLabel({
        matchId: "0195f3a0-0000-7000-8000-000000000002",
        kind: "interview",
        company: "Halcyon Labs",
        title: "Applied Scientist",
        receivedAt: "2026-11-08T12:00:00Z",
      }),
    ).toBe("An interview invitation for Applied Scientist at Halcyon Labs");
  });
});
