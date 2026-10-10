import { describe, expect, it } from "vitest";

import { RESUME_MAX_BYTES, resumeTooLarge } from "@/lib/uploads";

const MB = 1024 * 1024;

describe("resumeTooLarge", () => {
  it("allows up to exactly 10 MB, as Django does", () => {
    expect(RESUME_MAX_BYTES).toBe(10 * MB);
    expect(resumeTooLarge(0)).toBeNull();
    expect(resumeTooLarge(10 * MB)).toBeNull();
  });

  it("refuses anything larger with Django's sentence and rounding", () => {
    // The same cases as backend/tests/resumes/test_upload.py.
    expect(resumeTooLarge(10 * MB + 9)).toBe("That file is 10.0 MB; the limit is 10 MB.");
    expect(resumeTooLarge(Math.round(12.3 * MB))).toBe("That file is 12.3 MB; the limit is 10 MB.");
    expect(resumeTooLarge(10.25 * MB)).toBe("That file is 10.3 MB; the limit is 10 MB.");
    // Past Next's 16 MB proxy buffer, where the request would otherwise hang.
    expect(resumeTooLarge(20 * MB)).toBe("That file is 20.0 MB; the limit is 10 MB.");
  });
});
