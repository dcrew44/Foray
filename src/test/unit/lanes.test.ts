import { describe, expect, it } from "vitest";

import {
  isLane,
  LANE_HINT,
  LANE_LABEL,
  laneLabel,
  laneNote,
  LANES,
  matchesHref,
} from "@/lib/lanes";

describe("lanes", () => {
  it("names every lane Django can send, in the order a picker lists them", () => {
    expect(LANES).toEqual(["coached", "automated", "none"]);
    expect(LANES.map((lane) => LANE_LABEL[lane])).toEqual(["Coached", "Automated", "No lane"]);
  });

  it("knows a lane from any other string", () => {
    expect(LANES.every(isLane)).toBe(true);
    expect(["Coached", "review", "all", "toString", "", null, undefined].some(isLane)).toBe(false);
  });

  it("says what each lane asks of the user, and nothing for no lane", () => {
    expect(LANE_HINT).toEqual({
      coached: "Foray coaches, you write and send",
      automated: "Foray fills the form; you press submit",
      none: null,
    });
  });
});

describe("laneLabel", () => {
  it("is the lane's name on a row", () => {
    expect([laneLabel("coached"), laneLabel("automated")]).toEqual(["Coached", "Automated"]);
  });

  it("says nothing for a match in no lane, or for anything that is not a lane", () => {
    expect(["none", "vip", "", null, undefined].map(laneLabel)).toEqual(Array(5).fill(null));
  });
});

describe("laneNote", () => {
  it("says what the lane asks of the user", () => {
    expect(laneNote("coached")).toBe("Foray coaches, you write and send.");
    expect(laneNote("automated")).toBe("Foray fills the form; you press submit.");
  });

  it("says when the lane is the user's choice", () => {
    expect(laneNote("coached", true)).toBe("Foray coaches, you write and send. Your choice.");
    expect(laneNote("automated", true)).toBe(
      "Foray fills the form; you press submit. Your choice.",
    );
  });

  it("has nothing to add to no lane, unless the user chose that", () => {
    expect(laneNote("none")).toBeNull();
    expect(laneNote("none", true)).toBe("Your choice.");
  });

  it("says nothing for what is not a lane", () => {
    expect([null, undefined, "", "vip"].map((lane) => laneNote(lane, true))).toEqual(
      Array(4).fill(null),
    );
  });
});

describe("matchesHref", () => {
  it("is the bare page for the board with no filter", () => {
    expect(matchesHref()).toBe("/matches");
    expect(matchesHref({ view: "board", stage: null, closed: false, lane: null })).toBe("/matches");
  });

  it("names the list, the closed columns and the lane as Django reads them", () => {
    expect(matchesHref({ view: "list" })).toBe("/matches?view=list");
    expect(matchesHref({ closed: true })).toBe("/matches?closed=1");
    expect(matchesHref({ lane: "coached" })).toBe("/matches?lane=coached");
    expect(matchesHref({ lane: "none", closed: true })).toBe("/matches?closed=1&lane=none");
    expect(matchesHref({ view: "list", lane: "automated" })).toBe(
      "/matches?view=list&lane=automated",
    );
  });

  it("lets a stage stand for the list, which is where a stage link lands", () => {
    expect(matchesHref({ view: "list", stage: "applied" })).toBe("/matches?stage=applied");
    expect(matchesHref({ view: "list", stage: "applied", lane: "coached" })).toBe(
      "/matches?stage=applied&lane=coached",
    );
  });

  it("leaves out the closed columns where they change nothing: the list shows every stage", () => {
    expect(matchesHref({ view: "list", closed: true })).toBe("/matches?view=list");
    expect(matchesHref({ view: "list", stage: "archived", closed: true })).toBe(
      "/matches?stage=archived",
    );
  });
});
