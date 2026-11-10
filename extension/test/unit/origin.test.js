import { describe, expect, it } from "vitest";

import { parseAddress } from "../../lib/origin.js";

describe("the Foray address on the options page", () => {
  it("takes an address as it is typed and keeps only its origin", () => {
    expect(parseAddress("http://localhost:3000")).toEqual({
      ok: true,
      origin: "http://localhost:3000",
      pattern: "http://localhost/*",
      plain: false,
    });
    expect(parseAddress("  https://foray.example.com/settings?tab=1 ")).toMatchObject({
      origin: "https://foray.example.com",
      pattern: "https://foray.example.com/*",
      plain: false,
    });
  });

  it("assumes http on this machine and https anywhere else", () => {
    expect(parseAddress("localhost:3000")).toMatchObject({ origin: "http://localhost:3000" });
    expect(parseAddress("127.0.0.1:8123")).toMatchObject({
      origin: "http://127.0.0.1:8123",
      pattern: "http://127.0.0.1/*",
    });
    expect(parseAddress("[::1]:3000")).toMatchObject({
      origin: "http://[::1]:3000",
      pattern: "http://[::1]/*",
    });
    expect(parseAddress("foray.example.com")).toMatchObject({ origin: "https://foray.example.com" });
  });

  it("says when the token would cross a network unencrypted", () => {
    expect(parseAddress("http://192.168.1.20:3000")).toMatchObject({ ok: true, plain: true });
    expect(parseAddress("http://foray.localhost:3000")).toMatchObject({ ok: true, plain: false });
    expect(parseAddress("https://192.168.1.20")).toMatchObject({ ok: true, plain: false });
  });

  it("refuses what is not an address Foray could be at", () => {
    for (const typed of ["", "   ", "not an address", "ftp://foray.example.com", "chrome://settings", "http://"]) {
      expect(parseAddress(typed).ok, typed).toBe(false);
    }
    expect(parseAddress("https://me:secret@foray.example.com").message).toMatch(/user name and password/);
  });
});
