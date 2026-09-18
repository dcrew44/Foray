import { describe, expect, it } from "vitest";

import {
  detectRemote,
  isPlausibleSalary,
  parseLocation,
  parseSalary,
  primaryLocation,
} from "@/lib/sources/normalize";

describe("parseLocation", () => {
  it("splits a US city, state pair", () => {
    expect(parseLocation("San Francisco, CA")).toMatchObject({
      city: "San Francisco",
      region: "CA",
      country: "US",
      metro: "bay-area",
    });
  });

  it("expands a spelled-out state", () => {
    expect(parseLocation("Austin, Texas")).toMatchObject({
      city: "Austin",
      region: "TX",
      country: "US",
      metro: null,
    });
  });

  it("recognises a metro named directly, with no city", () => {
    expect(parseLocation("SF Bay Area")).toMatchObject({
      city: null,
      metro: "bay-area",
    });
  });

  it("maps suburbs onto the same metro", () => {
    for (const place of ["Mountain View, CA", "Oakland, CA", "Palo Alto, CA"]) {
      expect(parseLocation(place).metro).toBe("bay-area");
    }
  });

  it("strips a trailing remote qualifier from the city", () => {
    expect(parseLocation("New York, NY (Remote)")).toMatchObject({
      city: "New York",
      region: "NY",
    });
    expect(parseLocation("Seattle, WA - Hybrid").city).toBe("Seattle");
  });

  it("keeps the source's own words in raw", () => {
    const raw = "  San Francisco, CA (Remote)  ";
    expect(parseLocation(raw).raw).toBe(raw);
  });

  it("leaves an unrecognised place unguessed rather than wrong", () => {
    const parsed = parseLocation("Remote");
    expect(parsed.metro).toBeNull();
    // A wrong metro silently drops a job out of a search; an unknown one only
    // fails the filter.
    expect(parsed.country).toBeNull();
  });

  it("handles a non-US location without inventing a US state", () => {
    expect(parseLocation("London, United Kingdom")).toMatchObject({
      city: "London",
      country: "GB",
      metro: null,
    });
  });
});

describe("primaryLocation", () => {
  it("prefers a location we recognise over the first one listed", () => {
    expect(
      primaryLocation(["Dublin, Ireland", "San Jose, CA"])?.metro,
    ).toBe("bay-area");
  });

  it("falls back to the first when none is recognised", () => {
    expect(primaryLocation(["Dublin, Ireland", "Berlin"])?.city).toBe("Dublin");
  });

  it("returns null for no locations at all", () => {
    expect(primaryLocation([])).toBeNull();
  });
});

describe("detectRemote", () => {
  it("trusts an explicit flag", () => {
    expect(detectRemote({ isRemoteFlag: true, location: "San Francisco" })).toBe(
      "remote",
    );
  });

  it("prefers hybrid over remote when both words appear", () => {
    // "Hybrid remote" is hybrid; reading it as remote would be a worse error.
    expect(detectRemote({ location: "Hybrid Remote - Austin, TX" })).toBe(
      "hybrid",
    );
  });

  it("reads remote out of the location or the title", () => {
    expect(detectRemote({ location: "Remote - US" })).toBe("remote");
    expect(
      detectRemote({ title: "Software Engineer (Remote)", location: "" }),
    ).toBe("remote");
  });

  it("treats a bare city as onsite", () => {
    expect(detectRemote({ location: "San Francisco, CA" })).toBe("onsite");
  });

  it("does not let a passing mention in the description outvote the location", () => {
    // Descriptions say "remote" constantly — "collaborate with remote teams".
    expect(
      detectRemote({
        location: "San Francisco, CA",
        description: "You will work with remote teams across the company.",
      }),
    ).toBe("onsite");
  });

  it("falls back to the description only when nothing stronger says anything", () => {
    expect(
      detectRemote({
        location: "",
        description: "This is a fully remote position.",
      }),
    ).toBe("remote");
  });

  it("says unknown rather than guessing", () => {
    expect(detectRemote({})).toBe("unknown");
  });
});

describe("parseSalary", () => {
  it("reads a plain annual range", () => {
    expect(parseSalary("The salary range is $150,000 - $200,000 per year")).toMatchObject(
      { min: 150_000, max: 200_000, currency: "USD", period: "year" },
    );
  });

  it("carries a k suffix across both ends of the range", () => {
    // "$180-220k" means 180k, not $180.
    expect(parseSalary("$180-220k annually")).toMatchObject({
      min: 180_000,
      max: 220_000,
    });
  });

  it("reads an hourly rate and labels the period", () => {
    expect(parseSalary("$55 - $70 per hour")).toMatchObject({
      min: 55,
      max: 70,
      period: "hour",
    });
  });

  it("picks up a non-dollar currency", () => {
    expect(parseSalary("£70,000 – £95,000 per year").currency).toBe("GBP");
    expect(parseSalary("Salary: 90,000 - 120,000 EUR annually").currency).toBe(
      "EUR",
    );
  });

  it("ignores a year range that is not money", () => {
    expect(parseSalary("Founded 2015 - 2020, we have grown fast")).toMatchObject(
      { min: null, max: null },
    );
  });

  it("ignores a headcount range", () => {
    expect(parseSalary("a team of 5 - 10 engineers").min).toBeNull();
  });

  it("ignores a backwards range", () => {
    expect(parseSalary("$200,000 - $150,000").min).toBeNull();
  });

  it("returns empty for text with no range at all", () => {
    expect(parseSalary("Competitive salary and equity")).toMatchObject({
      min: null,
      max: null,
      currency: null,
      period: null,
    });
    expect(parseSalary(null).min).toBeNull();
  });
});

describe("isPlausibleSalary", () => {
  it("rejects an annual salary that is really a figure in thousands", () => {
    // Ashby serves exactly this for some Vanta roles: the employer typed 90
    // meaning $90K. Trusting it would filter the job out of any search with a
    // realistic salary floor — worse than having no salary at all.
    expect(
      isPlausibleSalary({ min: 90, max: 90, currency: "USD", period: "year" }),
    ).toBe(false);
  });

  it("accepts a real annual range", () => {
    expect(
      isPlausibleSalary({
        min: 180_000,
        max: 220_000,
        currency: "USD",
        period: "year",
      }),
    ).toBe(true);
  });

  it("accepts an hourly range at hourly magnitudes", () => {
    expect(
      isPlausibleSalary({ min: 35, max: 38, currency: "CAD", period: "hour" }),
    ).toBe(true);
    // The same numbers labelled annual are not a salary.
    expect(
      isPlausibleSalary({ min: 35, max: 38, currency: "CAD", period: "year" }),
    ).toBe(false);
  });

  it("rejects a backwards or empty range", () => {
    expect(
      isPlausibleSalary({
        min: 200_000,
        max: 150_000,
        currency: "USD",
        period: "year",
      }),
    ).toBe(false);
    expect(
      isPlausibleSalary({ min: null, max: null, currency: null, period: null }),
    ).toBe(false);
  });
});
