import type {
  NormalizedLocation,
  NormalizedSalary,
  RemoteKind,
} from "./types";

/**
 * Turning what boards say into something we can filter on.
 *
 * Every function here keeps the source's own words in `raw` and treats its own
 * output as an interpretation. When a location cannot be recognised it stays
 * `null` rather than being guessed at — a wrong metro silently drops a job out
 * of a search, which is worse than an unknown one that simply fails the filter.
 */

const US_STATES: Record<string, string> = {
  alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR",
  california: "CA", colorado: "CO", connecticut: "CT", delaware: "DE",
  florida: "FL", georgia: "GA", hawaii: "HI", idaho: "ID", illinois: "IL",
  indiana: "IN", iowa: "IA", kansas: "KS", kentucky: "KY", louisiana: "LA",
  maine: "ME", maryland: "MD", massachusetts: "MA", michigan: "MI",
  minnesota: "MN", mississippi: "MS", missouri: "MO", montana: "MT",
  nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
  "new mexico": "NM", "new york": "NY", "north carolina": "NC",
  "north dakota": "ND", ohio: "OH", oklahoma: "OK", oregon: "OR",
  pennsylvania: "PA", "rhode island": "RI", "south carolina": "SC",
  "south dakota": "SD", tennessee: "TN", texas: "TX", utah: "UT",
  vermont: "VT", virginia: "VA", washington: "WA", "west virginia": "WV",
  wisconsin: "WI", wyoming: "WY", "district of columbia": "DC",
};

/**
 * Cities we group into a metro.
 *
 * Only the Bay Area for now, because that is what the user is searching. This
 * is a gazetteer, not a geocoder: it exists so "Mountain View" and "SF" answer
 * the same filter, and it grows by adding rows.
 */
const METROS: Array<{ metro: string; cities: string[] }> = [
  {
    metro: "bay-area",
    cities: [
      "san francisco", "sf", "south san francisco", "oakland", "berkeley",
      "emeryville", "alameda", "san mateo", "burlingame", "redwood city",
      "menlo park", "palo alto", "east palo alto", "mountain view",
      "sunnyvale", "santa clara", "san jose", "cupertino", "milpitas",
      "fremont", "hayward", "san bruno", "daly city", "foster city",
      "brisbane", "los altos", "campbell", "los gatos", "pleasanton",
      "walnut creek", "san rafael", "novato", "sausalito", "richmond",
      "bay area", "sf bay area", "san francisco bay area", "silicon valley",
      "peninsula", "south bay", "east bay", "north bay",
    ],
  },
];

const CITY_TO_METRO = new Map<string, string>(
  METROS.flatMap(({ metro, cities }) =>
    cities.map((city) => [city, metro] as const),
  ),
);

const REMOTE_WORDS =
  /\b(?:fully\s+remote|remote(?:\s*-?\s*(?:first|friendly|ok|eligible|us|usa|global|anywhere))?|work\s+from\s+home|wfh|distributed|virtual)\b/i;
const HYBRID_WORDS = /\b(?:hybrid|flexible\s+(?:work|location)|partially\s+remote)\b/i;
const ONSITE_WORDS = /\b(?:on-?site|in-?office|in-?person)\b/i;

/**
 * Remote status from whatever signals exist.
 *
 * Boards rarely have a clean flag, so this reads the location string and the
 * title, and only falls back to the description — which mentions "remote" in
 * passing far too often to be trusted on its own — when a caller passes it and
 * the stronger signals said nothing.
 */
export function detectRemote(input: {
  location?: string | null;
  title?: string | null;
  isRemoteFlag?: boolean | null;
  description?: string | null;
}): RemoteKind {
  if (input.isRemoteFlag === true) return "remote";

  const strong = `${input.location ?? ""} ${input.title ?? ""}`;
  if (HYBRID_WORDS.test(strong)) return "hybrid";
  if (REMOTE_WORDS.test(strong)) return "remote";
  if (ONSITE_WORDS.test(strong)) return "onsite";

  // A named city with no remote wording is an onsite role in practice.
  if (input.location && parseLocation(input.location).city) return "onsite";

  if (input.description) {
    const head = input.description.slice(0, 600);
    if (HYBRID_WORDS.test(head)) return "hybrid";
    if (REMOTE_WORDS.test(head)) return "remote";
  }

  return "unknown";
}

/** Trailing qualifiers boards append to a city, which are not part of it. */
const LOCATION_NOISE =
  /\s*[-–—(]\s*(?:remote|hybrid|on-?site|in-?office|flexible|multiple locations|various)[^)]*\)?\s*$/i;

export function parseLocation(raw: string): NormalizedLocation {
  const cleaned = raw.replace(LOCATION_NOISE, "").trim();
  const base: NormalizedLocation = {
    raw,
    city: null,
    region: null,
    country: null,
    metro: null,
  };

  if (!cleaned) return base;

  const lower = cleaned.toLowerCase();

  // A metro named directly ("SF Bay Area") has no city to extract.
  const directMetro = CITY_TO_METRO.get(lower);
  if (directMetro) return { ...base, metro: directMetro };

  const parts = cleaned
    .split(/\s*[,|/]\s*/)
    .map((part) => part.trim())
    .filter(Boolean);

  if (parts.length === 0) return base;

  const city = parts[0];
  const rest = parts.slice(1);

  let region: string | null = null;
  let country: string | null = null;

  for (const part of rest) {
    const key = part.toLowerCase();
    if (US_STATES[key]) {
      region = US_STATES[key];
      country = country ?? "US";
    } else if (/^[A-Z]{2}$/.test(part) && part !== "UK") {
      region = part;
      country = country ?? "US";
    } else if (/^(?:usa|u\.s\.a?\.?|united states)$/i.test(part)) {
      country = "US";
    } else if (/^(?:uk|united kingdom|england)$/i.test(part)) {
      country = "GB";
    } else if (!region) {
      region = part;
    } else {
      country = part;
    }
  }

  const metro =
    CITY_TO_METRO.get(city.toLowerCase()) ??
    CITY_TO_METRO.get(lower) ??
    null;

  return { raw, city, region, country, metro };
}

/** The first location we can make sense of, else the first one at all. */
export function primaryLocation(
  locations: string[],
): NormalizedLocation | null {
  if (locations.length === 0) return null;
  const parsed = locations.map(parseLocation);
  return parsed.find((location) => location.metro) ?? parsed[0];
}

const CURRENCIES: Record<string, string> = {
  $: "USD", "us$": "USD", usd: "USD",
  "£": "GBP", gbp: "GBP",
  "€": "EUR", eur: "EUR",
  "c$": "CAD", cad: "CAD",
};

/** "180,000" / "180k" / "180" (in a k-scaled range) → a number. */
function parseAmount(text: string): number | null {
  const match = /^([\d,.]+)\s*([km])?$/i.exec(text.trim());
  if (!match) return null;

  const value = Number(match[1].replace(/,/g, ""));
  if (!Number.isFinite(value)) return null;

  const suffix = match[2]?.toLowerCase();
  if (suffix === "k") return value * 1_000;
  if (suffix === "m") return value * 1_000_000;
  return value;
}

const PERIOD_WORDS: Array<[RegExp, NonNullable<NormalizedSalary["period"]>]> = [
  [/\b(?:per\s+hour|hourly|\/\s*(?:hr|hour)|an\s+hour)\b/i, "hour"],
  [/\b(?:per\s+month|monthly|\/\s*(?:mo|month)|a\s+month)\b/i, "month"],
  [/\b(?:per\s+year|annually|annual|\/\s*(?:yr|year)|a\s+year)\b/i, "year"],
];

/**
 * Salary out of free text.
 *
 * Structured compensation fields are preferred wherever a board offers them;
 * this is for the majority that only put the range in the description, usually
 * because a pay-transparency law made them.
 */
export function parseSalary(text: string | null | undefined): NormalizedSalary {
  const empty: NormalizedSalary = {
    min: null,
    max: null,
    currency: null,
    period: null,
  };
  if (!text) return empty;

  const range =
    /(?<currency>US\$|C\$|[$£€])?\s*(?<min>[\d][\d,.]*\s*[km]?)\s*(?:-|–|—|to)\s*(?<currency2>US\$|C\$|[$£€])?\s*(?<max>[\d][\d,.]*\s*[km]?)/i.exec(
      text,
    );

  if (!range?.groups) return empty;

  let min = parseAmount(range.groups.min);
  const max = parseAmount(range.groups.max);
  if (min === null || max === null) return empty;

  // "180-220k" means 180k, not 180 — the suffix carries across the range.
  if (max >= 1000 && min < 1000 && /[km]/i.test(range.groups.max)) {
    min *= max >= 1_000_000 ? 1_000_000 : 1_000;
  }

  if (min <= 0 || min > max) return empty;

  const symbol = (range.groups.currency ?? range.groups.currency2 ?? "")
    .toLowerCase();
  const nearby = text.slice(Math.max(0, range.index - 40), range.index + 120);
  const code = /\b(USD|GBP|EUR|CAD)\b/i.exec(nearby)?.[1]?.toLowerCase() ?? "";
  const currency = CURRENCIES[symbol] ?? CURRENCIES[code] ?? null;

  const periodWord =
    PERIOD_WORDS.find(([pattern]) => pattern.test(nearby))?.[1] ?? null;

  /**
   * Two numbers with a dash between them are usually not money. Job posts are
   * full of year ranges, headcounts and version numbers, and a salary invented
   * from one of those would filter real jobs out of a search. So a range is
   * only read as pay when something nearby says it is: a currency, a period, or
   * an explicit pay word.
   */
  const moneyWord =
    /\b(?:salary|salaries|compensation|comp|pay|paid|wage|base|ote|remuneration|stipend)\b/i.test(
      nearby,
    );
  if (!currency && !periodWord && !moneyWord) return empty;

  const period =
    periodWord ??
    // An hourly rate is the only way a genuine pay range lands this low.
    (max < 1000 ? "hour" : "year");

  if (!isPlausibleSalary({ min, max, currency, period })) return empty;

  return { min, max, currency, period };
}

/**
 * Does this range plausibly describe pay for the period it claims?
 *
 * Applied to structured compensation fields too, not just text. Ashby will
 * happily serve `{compensationType: "Salary", interval: "1 YEAR", minValue: 90}`
 * because the employer typed the figure in thousands — and a $90 salary floor
 * is worse than no salary at all, since it filters the job out of every search
 * with a realistic minimum rather than merely failing to match one.
 */
export function isPlausibleSalary(salary: NormalizedSalary): boolean {
  const { min, max, period } = salary;
  if (min === null || max === null) return false;
  if (min <= 0 || min > max) return false;

  switch (period) {
    case "hour":
      return min >= 5 && max <= 2_000;
    case "month":
      return min >= 500 && max <= 200_000;
    case "year":
      return min >= 15_000 && max <= 10_000_000;
    default:
      // No period stated: accept only magnitudes that could be an annual
      // figure, since that is what an unlabelled range almost always is.
      return min >= 15_000 && max <= 10_000_000;
  }
}
