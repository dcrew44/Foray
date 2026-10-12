import { describe, expect, it } from "vitest";

import { DEFAULT_NEXT, safeNext } from "@/lib/safe-next";

const SITE = "http://site.example";

/** Where a browser on SITE goes when it follows `href`. */
function origin(href: string): string {
  return new URL(href, SITE).origin;
}

// Browsers read "\\" as "/" and drop tabs and newlines, so each of these leaves the site as is.
const LEAVING = [
  "/\\evil.example/login",
  "/\\\\evil.example",
  "/\t/evil.example",
  "/\n/evil.example",
  "/\r/evil.example",
];

// Each of these stays on the site as typed, but resolving it collapses the dot segments into a
// leading "//", which a browser then reads as another site.
const COLLAPSING = [
  "/.//evil.example",
  "/a/..//evil.example/login",
  "/%2e%2e//evil.example",
  "/%2E//evil.example",
  "/.%2e//evil.example",
  "/././/evil.example?x=1#h",
];

const ODD = ["/ /evil.example", "/\u0000/evil.example", "/\u007f/evil.example"];

const KEPT = [
  "/matches",
  "/matches?x=1#h",
  "/a/../b",
  "/a//b",
  "/%5Cevil.example",
  "/x?next=//evil.example",
  "/./matches",
];

describe("safeNext", () => {
  it("keeps a path on this site, with its query and fragment", () => {
    expect(safeNext("/matches")).toBe("/matches");
    expect(safeNext("/matches?x=1#h")).toBe("/matches?x=1#h");
    expect(safeNext("/chat/abc")).toBe("/chat/abc");
    expect(safeNext("/matches?view=board&stage=applied")).toBe("/matches?view=board&stage=applied");
    expect(safeNext("/a/../b")).toBe("/b");
    expect(safeNext("/./matches")).toBe("/matches");
    expect(safeNext("/a//b")).toBe("/a//b");
  });

  it("sends anything else home", () => {
    expect(safeNext(undefined)).toBe(DEFAULT_NEXT);
    expect(safeNext(null)).toBe(DEFAULT_NEXT);
    expect(safeNext("")).toBe(DEFAULT_NEXT);
    expect(safeNext(["/matches", "/home"])).toBe(DEFAULT_NEXT);
    expect(safeNext("matches")).toBe(DEFAULT_NEXT);
    expect(safeNext("https://evil.example/")).toBe(DEFAULT_NEXT);
    expect(safeNext("//evil.example")).toBe(DEFAULT_NEXT);
  });

  it("refuses what a browser would read as another site", () => {
    for (const crafted of LEAVING) {
      expect(new URL(crafted, SITE).hostname, JSON.stringify(crafted)).toBe("evil.example");
      expect(safeNext(crafted), JSON.stringify(crafted)).toBe(DEFAULT_NEXT);
    }
    // Spaces and the other control characters are refused too: no real path needs them raw.
    for (const odd of ODD) {
      expect(safeNext(odd), JSON.stringify(odd)).toBe(DEFAULT_NEXT);
    }
  });

  it("refuses a path whose dot segments collapse into another site", () => {
    for (const crafted of COLLAPSING) {
      // Typed as is, the browser stays here; the resolved path is what would leave.
      expect(origin(crafted), JSON.stringify(crafted)).toBe(SITE);
      const resolved = new URL(crafted, SITE);
      expect(origin(resolved.pathname), JSON.stringify(crafted)).not.toBe(SITE);
      expect(safeNext(crafted), JSON.stringify(crafted)).toBe(DEFAULT_NEXT);
    }
  });

  it("never returns a link that leaves the site", () => {
    // Every value above, and every short path built from dot segments, slashes and a host name:
    // whatever comes back has one leading slash and resolves to this site.
    const parts = ["", ".", "..", "%2e", "%2E%2e", ".%2e", "a", "evil.example", "\\", "%5C"];
    const built: string[] = [];
    for (const a of parts) for (const b of parts) for (const c of parts) built.push(`/${a}/${b}/${c}`);
    for (const crafted of [...LEAVING, ...COLLAPSING, ...ODD, ...KEPT, ...built]) {
      const result = safeNext(crafted);
      expect(result.startsWith("/") && !result.startsWith("//"), JSON.stringify(crafted)).toBe(true);
      expect(origin(result), JSON.stringify(crafted)).toBe(SITE);
    }
    for (const kept of KEPT) {
      expect(safeNext(kept), JSON.stringify(kept)).not.toBe(DEFAULT_NEXT);
    }
  });
});
