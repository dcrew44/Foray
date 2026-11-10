import { describe, expect, it } from "vitest";

// A content script, not a module: it hangs its functions on `globalThis.Foray`.
import "../../content/text.js";

const T = globalThis.Foray.text;

describe("labels", () => {
  it("reads a label without the mark that says it is required", () => {
    expect(T.cleanLabel("  First   Name * ")).toBe("First Name");
    expect(T.cleanLabel("Full name✱")).toBe("Full name");
    expect(T.cleanLabel("Resume (required)")).toBe("Resume");
    expect(T.cleanLabel("Phone (optional)")).toBe("Phone");
    expect(T.saysRequired("Email *")).toBe(true);
    expect(T.saysRequired("Resume (required)")).toBe(true);
    expect(T.saysRequired("Email")).toBe(false);
  });

  it("matches the same question whatever its case, punctuation and required mark", () => {
    expect(T.labelScore("First Name", "first name *")).toBe(3);
    expect(T.labelScore("Why do you want to join?", "Why do you want to join")).toBe(3);
    expect(T.labelScore("Resume", "Resume/CV")).toBe(2);
    expect(T.labelScore("LinkedIn", "LinkedIn profile")).toBe(2);
  });

  it("does not take one question for another that starts the same way", () => {
    expect(T.labelScore("Name", "Name of your current employer")).toBe(0);
    expect(T.labelScore("First name", "Preferred first name")).toBe(0);
    expect(T.labelScore("Phone", "Emergency contact phone")).toBe(0);
    expect(T.labelScore("", "Email")).toBe(0);
  });

  it("picks the best label, and none when two fit equally", () => {
    expect(T.bestLabel("Email", ["Phone", "Email *", "Email address"])).toBe(1);
    expect(T.bestLabel("Resume", ["Cover letter", "Resume/CV"])).toBe(1);
    expect(T.bestLabel("Location", ["Location", "Location"])).toBe(-1);
    expect(T.bestLabel("Salary", ["Phone", "Email"])).toBe(-1);
  });
});

describe("options", () => {
  it("chooses the option that says the value", () => {
    expect(T.matchOption("No", ["Yes", "No"])).toBe(1);
    expect(T.matchOption("decline to self identify", ["Male", "Decline To Self Identify"])).toBe(1);
    expect(T.matchOption("I don’t wish to answer", ["Yes", "I don't wish to answer"])).toBe(1);
  });

  it("takes the one option the value begins, or that begins the value", () => {
    const longer = ["Yes, I am authorized to work here", "No, I am not"];
    expect(T.matchOption("Yes", longer)).toBe(0);
    expect(T.matchOption("No", longer)).toBe(1);
    expect(T.matchOption("Yes, I will need sponsorship in the future", ["Yes", "No"])).toBe(0);
  });

  it("prefers the same words over an option that only begins with them", () => {
    expect(T.matchOption("No", ["No, never", "No"])).toBe(1);
  });

  it("does not guess", () => {
    expect(T.matchOption("No", ["Not sure", "None of these"])).toBe(-1); // a word, not a prefix
    expect(T.matchOption("Yes", ["Yes, with sponsorship", "Yes, without sponsorship"])).toBe(-1);
    expect(T.matchOption("Berlin", ["Lisbon", "Toronto"])).toBe(-1);
    expect(T.matchOption("Decline to answer", ["Prefer not to say"])).toBe(-1);
    expect(T.matchOption("", ["", "Yes"])).toBe(-1);
  });
});

describe("places", () => {
  const suggestions = [
    "Portland, Oregon, United States",
    "Portland, Maine, United States",
    "Portland, Victoria, Australia",
    "South Portland, Maine, United States",
  ];

  it("chooses the suggestion for the city and state given, by either name of the state", () => {
    expect(T.matchLocation("Portland, ME", suggestions)).toBe(1);
    expect(T.matchLocation("Portland, Maine", suggestions)).toBe(1);
    expect(T.matchLocation("Portland, OR, United States", suggestions)).toBe(0);
  });

  it("takes the site's first suggestion when only a city was given", () => {
    expect(T.matchLocation("Portland", suggestions)).toBe(0);
  });

  it("does not take a different city or a place the list does not have", () => {
    expect(T.matchLocation("Portland, TX", suggestions)).toBe(-1);
    expect(T.matchLocation("Port", suggestions)).toBe(-1);
    expect(T.matchLocation("", suggestions)).toBe(-1);
  });
});

describe("agreements", () => {
  it("knows a lone box that agrees to something", () => {
    for (const label of [
      "I agree to the terms of service",
      "I have read and accept the privacy notice",
      "Yes, Northwind can contact me about future opportunities",
      "Email me the newsletter",
      "I understand that offers depend on a background check",
      "I acknowledge the candidate privacy policy",
    ]) {
      expect(T.isConsent({ label, options: [label] }), label).toBe(true);
    }
    expect(T.isConsent({ key: "consent[marketing]", label: "Yes", options: ["Yes"] })).toBe(true);
    expect(T.isConsent({ label: "Do you agree?", options: ["I agree"] })).toBe(true);
  });

  it("does not take a real question for an agreement", () => {
    expect(T.isConsent({ label: "Which teams interest you?", options: ["Platform", "Data"] })).toBe(false);
    expect(T.isConsent({ label: "I currently work in fintech", options: ["I currently work in fintech"] })).toBe(false);
    // Two options is a choice, even when it is about terms.
    expect(T.isConsent({ label: "Do you accept the terms?", options: ["Yes", "No"] })).toBe(false);
  });
});

describe("submit and confirmation", () => {
  it("knows the words on a submit button", () => {
    for (const words of ["Submit application", "Submit Application", "Submit", "Send application", "Apply", "Apply now"]) {
      expect(T.isSubmitText(words), words).toBe(true);
    }
    for (const words of ["Attach", "Apply with LinkedIn", "Save for later", "Submit a referral", "Next"]) {
      expect(T.isSubmitText(words), words).toBe(false);
    }
  });

  it("takes only plain statements that an application went in", () => {
    for (const words of [
      "Thank you for applying.",
      "Thanks for applying!",
      "Application submitted!",
      "Your application has been received.",
      "Your application was successfully submitted.",
      "We've received your application",
    ]) {
      expect(T.saysConfirmed(words), words).toBe(true);
    }
    for (const words of [
      "Thank you for your interest in Northwind",
      "Submit your application",
      "Applications are received until Friday",
      "Apply for this job",
      "Your application could not be submitted",
    ]) {
      expect(T.saysConfirmed(words), words).toBe(false);
    }
  });
});

describe("small things", () => {
  it("knows an application id from anything else", () => {
    expect(T.isUuid("0c7b8e7e-0000-4000-8000-000000000001")).toBe(true);
    expect(T.isUuid("../me")).toBe(false);
  });

  it("counts in words", () => {
    expect(T.plural(1, "answer")).toBe("1 answer");
    expect(T.plural(3, "box", "boxes")).toBe("3 boxes");
  });
});
