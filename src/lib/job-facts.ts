import type { components } from "@/lib/api/schema";

/**
 * What a posting asks of whoever applies, in words. The facts are read once per posting on the
 * backend (`apps/jobs/facts.py`); this only names them.
 */
export type JobFacts = components["schemas"]["JobFacts"];

const LEVEL: Record<JobFacts["level"], string> = {
  intern: "Internship",
  new_grad: "New graduate role",
  entry: "Entry level",
  mid: "Mid level",
  senior: "Senior",
  staff_plus: "Staff or above",
  unknown: "Not stated",
};

const LEVEL_FROM: Record<NonNullable<JobFacts["levelFrom"]>, string> = {
  title: "from its title",
  text: "in the posting's own words",
  years: "from the years it asks for",
};

export function levelLabel(facts: Pick<JobFacts, "level" | "levelFrom">): string {
  const level = LEVEL[facts.level];
  return facts.levelFrom && facts.level !== "unknown"
    ? `${level}, ${LEVEL_FROM[facts.levelFrom]}`
    : level;
}

function years(count: number): string {
  return count === 1 ? "1 year" : `${count} years`;
}

/** "3 or more years required", "None required", "5 or more years preferred", "Not stated". */
export function yearsLabel(facts: Pick<JobFacts, "yearsMin" | "yearsPreferred">): string {
  if (facts.yearsMin === 0) return "None required";
  if (facts.yearsMin != null) return `${years(facts.yearsMin)} or more required`;
  if (facts.yearsPreferred != null && facts.yearsPreferred > 0) {
    return `${years(facts.yearsPreferred)} or more preferred, none required`;
  }
  return "Not stated";
}

const WORK_AUTH: Record<JobFacts["workAuth"], string> = {
  citizen_only: "US citizens only",
  us_person: "US persons only: citizens, permanent residents, refugees and asylees",
  no_sponsorship: "No visa sponsorship",
  sponsors: "Sponsors visas",
  unknown: "Not stated",
};

export function workAuthLabel(facts: Pick<JobFacts, "workAuth">): string {
  return WORK_AUTH[facts.workAuth];
}

const CLEARANCE: Record<JobFacts["clearance"], string> = {
  required: "Needs a security clearance you already hold",
  eligible: "Needs a security clearance you can obtain",
  none: "None mentioned",
};

export function clearanceLabel(facts: Pick<JobFacts, "clearance">): string {
  return CLEARANCE[facts.clearance];
}
