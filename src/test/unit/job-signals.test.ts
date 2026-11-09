import { describe, expect, it } from "vitest";

import { splitSignals, type JobSignal } from "@/lib/job-signals";

const CLOSED: JobSignal = {
  kind: "closed",
  label: "Closed",
  detail: "Last seen on the employer's board on November 3.",
};
const LINK: JobSignal = {
  kind: "aggregator_link",
  label: "Apply link goes elsewhere",
  detail: "The apply link goes to example-jobs.com, not the employer's site.",
};
const REPOSTED: JobSignal = {
  kind: "reposted",
  label: "Reposted",
  detail: "Posted 4 times since May.",
};
const LONG_OPEN: JobSignal = {
  kind: "long_open",
  label: "Open a long time",
  detail: "Open for 74 days.",
};

describe("splitSignals", () => {
  it("has nothing to show for an ordinary posting", () => {
    expect(splitSignals([])).toEqual({ blocking: [], notes: [] });
  });

  it("makes only a closed posting prominent", () => {
    expect(splitSignals([CLOSED, LINK, REPOSTED, LONG_OPEN])).toEqual({
      blocking: [CLOSED],
      notes: [LINK, REPOSTED, LONG_OPEN],
    });
  });

  it("keeps the notes in the order the backend sent them", () => {
    expect(splitSignals([LINK, LONG_OPEN]).notes).toEqual([LINK, LONG_OPEN]);
    expect(splitSignals([REPOSTED, LONG_OPEN])).toEqual({
      blocking: [],
      notes: [REPOSTED, LONG_OPEN],
    });
  });

  it("leaves the list it was given alone", () => {
    const given = [CLOSED, REPOSTED];
    splitSignals(given);
    expect(given).toEqual([CLOSED, REPOSTED]);
  });
});
