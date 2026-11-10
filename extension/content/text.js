/*
 * Words: how a label, an option or a page's text is compared. Pure functions with no DOM, so
 * they are unit-tested in Node (extension/test/unit/text.test.js).
 *
 * Content scripts are classic scripts, not modules, so every file under content/ hangs what it
 * offers on one object, `Foray`, in the extension's own isolated world. The page cannot see it.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.text) return;

  /** Text as it is compared: one case, one kind of quote and dash, no punctuation. */
  function norm(value) {
    return String(value ?? "")
      .normalize("NFKC")
      .toLowerCase()
      .replace(/[‘’ʼ]/g, "'")
      .replace(/[“”]/g, '"')
      .replace(/[‐-―]/g, "-")
      .replace(/[^\p{L}\p{N}']+/gu, " ")
      .trim();
  }

  /** A label as a person reads it: one line, without the mark that says it is required. */
  function cleanLabel(value) {
    return String(value ?? "")
      .replace(/\s+/g, " ")
      .replace(/\s*\((?:required|optional)\)\s*$/i, "")
      .replace(/\s*[*✱∗⁎]+\s*$/u, "")
      .trim();
  }

  function saysRequired(value) {
    return /[*✱∗⁎]\s*$|\(required\)\s*$/iu.test(String(value ?? "").trim());
  }

  /**
   * How well a label on the page answers to the label Foray planned for: 3 the same words,
   * 2 one is the other with more words after it, 0 not the same question.
   */
  function labelScore(wanted, found) {
    const a = norm(cleanLabel(wanted));
    const b = norm(cleanLabel(found));
    if (!a || !b) return 0;
    if (a === b) return 3;
    const [short, long] = a.length <= b.length ? [a, b] : [b, a];
    // "Resume" against "Resume/CV", "LinkedIn" against "LinkedIn profile". A short label
    // must be most of the long one, or "Name" would answer to "Name of your school".
    if (long.startsWith(`${short} `) && short.length * 2 >= long.length) return 2;
    return 0;
  }

  /** The label in `found` that best answers to `wanted`, or -1 when none or two tie. */
  function bestLabel(wanted, found) {
    let best = -1;
    let score = 0;
    let tied = false;
    found.forEach((label, index) => {
      const s = labelScore(wanted, label);
      if (s > score) {
        best = index;
        score = s;
        tied = false;
      } else if (s === score && s > 0) {
        tied = true;
      }
    });
    return score > 0 && !tied ? best : -1;
  }

  /**
   * Which of a control's options says `value`. Foray chose the value from the form's own
   * options whenever it could read them, so this is nearly always the same words. Otherwise
   * exactly one option may begin with the value ("Yes" for "Yes, I am authorized") or be
   * what the value begins with. Anything less certain is no match: the field is left for
   * the user rather than guessed.
   */
  function matchOption(value, options) {
    const wanted = norm(value);
    if (!wanted) return -1;
    const said = options.map(norm);
    const exact = said.indexOf(wanted);
    if (exact !== -1) return exact;
    const begins = (long, short) => short && long.startsWith(`${short} `);
    const longer = said.flatMap((option, index) => (begins(option, wanted) ? [index] : []));
    if (longer.length === 1) return longer[0];
    if (longer.length > 1) return -1;
    const shorter = said.flatMap((option, index) => (begins(wanted, option) ? [index] : []));
    return shorter.length === 1 ? shorter[0] : -1;
  }

  const US_STATES = {
    al: "alabama", ak: "alaska", az: "arizona", ar: "arkansas", ca: "california",
    co: "colorado", ct: "connecticut", de: "delaware", dc: "district of columbia",
    fl: "florida", ga: "georgia", hi: "hawaii", id: "idaho", il: "illinois", in: "indiana",
    ia: "iowa", ks: "kansas", ky: "kentucky", la: "louisiana", me: "maine", md: "maryland",
    ma: "massachusetts", mi: "michigan", mn: "minnesota", ms: "mississippi", mo: "missouri",
    mt: "montana", ne: "nebraska", nv: "nevada", nh: "new hampshire", nj: "new jersey",
    nm: "new mexico", ny: "new york", nc: "north carolina", nd: "north dakota", oh: "ohio",
    ok: "oklahoma", or: "oregon", pa: "pennsylvania", ri: "rhode island",
    sc: "south carolina", sd: "south dakota", tn: "tennessee", tx: "texas", ut: "utah",
    vt: "vermont", va: "virginia", wa: "washington", wv: "west virginia", wi: "wisconsin",
    wy: "wyoming",
  };

  function placeParts(value) {
    return String(value ?? "")
      .split(",")
      .map(norm)
      .filter(Boolean);
  }

  /**
   * Which suggestion of a location search is the place the user gave. The city must lead the
   * suggestion, and whatever else they gave (a state, a country) must be in it, a US state
   * by either of its names. The first suggestion that qualifies wins: the list is the
   * site's own ranking.
   */
  function matchLocation(value, options) {
    const [city, ...rest] = placeParts(value);
    if (!city) return -1;
    for (let index = 0; index < options.length; index += 1) {
      const parts = placeParts(options[index]);
      if (parts[0] !== city) continue;
      const others = parts.slice(1);
      const has = (part) => others.includes(part) || others.includes(US_STATES[part] ?? "");
      if (rest.every(has)) return index;
    }
    return -1;
  }

  const CONSENT =
    /\b(i agree|i accept|i consent|i acknowledge|i understand|i certify|i confirm|i have read|agree to|consent|terms|privacy|policy|gdpr|data protection|personal data|personal information|marketing|newsletter|subscribe|contact me|keep me|talent (community|pool|network)|future (opportunities|openings|roles|positions)|acknowledg\w*)\b/;
  const CONSENT_NAME = /consent|terms|privacy|gdpr|agree|marketing|newsletter|opt[_-]?in/i;

  /**
   * Whether a lone tick box is an agreement: terms, a privacy notice, marketing, an
   * acknowledgement. Foray never ticks those; they are the user's to read and tick.
   */
  function isConsent({ key = "", label = "", options = [] }) {
    if (options.length > 1) return false;
    if (CONSENT_NAME.test(key)) return true;
    return CONSENT.test(norm(`${label} ${options[0] ?? ""}`));
  }

  const SUBMIT =
    /^(submit|send|finish|complete)( (my|your|the))?( application| form)?$|^apply( now)?$|^submit and apply$/;

  /** Whether a button's words say it sends the application. */
  function isSubmitText(value) {
    return SUBMIT.test(norm(value));
  }

  const CONFIRMED = [
    /\bthank(s| you) for (applying|your application|submitting your application)\b/,
    /\b(your )?application (has been|was|is) (successfully )?(submitted|received|sent)\b/,
    /\bapplication (submitted|received|sent|complete)\b/,
    /\bwe(?: have|'ve)? (successfully )?received your application\b/,
    /\bsuccessfully (applied|submitted your application)\b/,
  ];

  /** Whether a page's words say an application went in. Deliberately few phrases. */
  function saysConfirmed(value) {
    const text = norm(value);
    return CONFIRMED.some((pattern) => pattern.test(text));
  }

  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function slug(value) {
    return norm(value).replace(/ /g, "-").slice(0, 80);
  }

  function plural(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
  }

  Foray.text = {
    norm,
    cleanLabel,
    saysRequired,
    labelScore,
    bestLabel,
    matchOption,
    matchLocation,
    isConsent,
    isSubmitText,
    saysConfirmed,
    slug,
    plural,
    isUuid: (value) => UUID.test(String(value ?? "")),
  };
})();
