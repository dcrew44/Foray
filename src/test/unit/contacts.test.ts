import { describe, expect, it } from "vitest";

import {
  NOT_A_CONTACT_LIST,
  connectedLabel,
  contactsFileProblem,
  knowsLabel,
} from "@/lib/contacts";

const MB = 1024 * 1024;

describe("contactsFileProblem", () => {
  it("lets a list or LinkedIn's archive through to Django", () => {
    expect(contactsFileProblem("Connections.csv", 40_000)).toBeNull();
    expect(contactsFileProblem("Basic_LinkedInDataExport_11-09-2026.zip", 2 * MB)).toBeNull();
    expect(contactsFileProblem("alumni.TSV", 10 * MB)).toBeNull();
  });

  it("refuses other files with Django's sentence, before anything is sent", () => {
    // The same sentence as backend/apps/contacts/parse.py NOT_A_LIST.
    expect(NOT_A_CONTACT_LIST).toBe(
      "Upload a CSV file: LinkedIn's Connections.csv, or any list with a name column.",
    );
    expect(contactsFileProblem("resume.pdf", 40_000)).toBe(NOT_A_CONTACT_LIST);
    expect(contactsFileProblem("contacts.xlsx", 40_000)).toBe(NOT_A_CONTACT_LIST);
    expect(contactsFileProblem("Connections", 40_000)).toBe(NOT_A_CONTACT_LIST);
  });

  it("refuses a file over the upload limit with its size", () => {
    expect(contactsFileProblem("Connections.csv", Math.round(12.3 * MB))).toBe(
      "That file is 12.3 MB; the limit is 10 MB.",
    );
  });
});

describe("knowsLabel", () => {
  const person = (name: string) => ({ name });

  it("says nothing where the user knows nobody", () => {
    expect(knowsLabel(null)).toBeNull();
    expect(knowsLabel(undefined)).toBeNull();
    expect(knowsLabel({ count: 0, people: [] })).toBeNull();
  });

  it("names one person, two people, or the first and how many others", () => {
    expect(knowsLabel({ count: 1, people: [person("Priya Rao")] })).toBe("You know Priya Rao");
    expect(knowsLabel({ count: 2, people: [person("Priya Rao"), person("Marcus Bell")] })).toBe(
      "You know Priya Rao and Marcus Bell",
    );
    expect(
      knowsLabel({ count: 3, people: [person("Priya Rao"), person("Marcus Bell"), person("Ines Ortega")] }),
    ).toBe("You know Priya Rao and 2 others");
    // The row carries three names at most; the count is of everyone.
    expect(knowsLabel({ count: 41, people: [person("Priya Rao")] })).toBe(
      "You know Priya Rao and 40 others",
    );
    expect(knowsLabel({ count: 2, people: [person("Priya Rao")] })).toBe(
      "You know Priya Rao and 1 other",
    );
  });

  it("falls back to the count when no name came with it", () => {
    expect(knowsLabel({ count: 1, people: [] })).toBe("You know 1 person here");
    expect(knowsLabel({ count: 5, people: [] })).toBe("You know 5 people here");
  });
});

describe("connectedLabel", () => {
  it("names the month and year a file says two people connected", () => {
    expect(connectedLabel("2024-03-15")).toBe("Connected Mar 2024");
    expect(connectedLabel("2017-12-01")).toBe("Connected Dec 2017");
    // A calendar date, not an instant: the first of the month stays in its month everywhere.
    expect(connectedLabel("2026-01-01")).toBe("Connected Jan 2026");
  });

  it("says nothing for no date or one it cannot read", () => {
    expect(connectedLabel(null)).toBeNull();
    expect(connectedLabel(undefined)).toBeNull();
    expect(connectedLabel("")).toBeNull();
    expect(connectedLabel("2024-13-01")).toBeNull();
    expect(connectedLabel("15 Mar 2024")).toBeNull();
  });
});
