import { cn } from "@/lib/utils";
import {
  fitLevel,
  fitWord,
  initials,
  nameHue,
  RECOMMENDATION_LABEL,
  type RecommendationId,
} from "@/lib/format";

/**
 * The small visual vocabulary every page shares.
 *
 * Server-safe: no hooks, no client state, so pages can render these directly.
 */

/**
 * The mark: a trail blaze — the painted rectangle that tells a hiker they are
 * still on the path — with a notch of signal marigold. It pulses only while
 * agents are working.
 */
export function Blaze({
  className,
  live = false,
}: {
  className?: string;
  live?: boolean;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "relative inline-flex h-[18px] w-[11px] shrink-0 rounded-[3px] bg-primary",
        className,
      )}
    >
      <span
        className={cn(
          "absolute -top-[3px] -right-[3px] size-[7px] rounded-full bg-signal ring-2 ring-canvas",
          live && "animate-signal",
        )}
      />
    </span>
  );
}

export function Wordmark({
  className,
  live,
}: {
  className?: string;
  live?: boolean;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <Blaze live={live} />
      <span className="text-display text-[19px] leading-none tracking-[-0.03em]">
        Foray
      </span>
    </span>
  );
}

const FIT_CLASS = [
  "bg-fit-0 text-fit-0-foreground",
  "bg-fit-1 text-fit-1-foreground",
  "bg-fit-2 text-fit-2-foreground",
  "bg-fit-3 text-fit-3-foreground",
  "bg-fit-4 text-fit-4-foreground",
  "bg-fit-5 text-fit-5-foreground",
] as const;

export function fitClass(score: number | null | undefined) {
  return FIT_CLASS[fitLevel(score)];
}

/**
 * A match score as a chip whose green deepens with fit. The number is set in
 * the display face; unscored shows a quiet dash rather than a zero.
 */
export function ScoreChip({
  score,
  size = "md",
  className,
  evaluated,
}: {
  score: number | null | undefined;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
  /** Marks a score that came from the deep evaluation rather than triage. */
  evaluated?: boolean;
}) {
  const dims = {
    sm: "size-7 rounded-md text-[12px]",
    md: "size-9 rounded-lg text-[15px]",
    lg: "size-12 rounded-xl text-[20px]",
    xl: "size-[72px] rounded-2xl text-[32px]",
  }[size];

  return (
    <span
      className={cn(
        "text-display relative inline-flex shrink-0 items-center justify-center tabular leading-none",
        dims,
        fitClass(score),
        className,
      )}
      title={score == null ? "Not scored yet" : `${fitWord(score)} (${score})`}
    >
      {score == null ? "–" : score}
      {evaluated && size !== "sm" && (
        <span
          aria-label="Deep-evaluated"
          className="absolute -right-0.5 -bottom-0.5 size-2 rounded-full bg-signal ring-2 ring-background"
        />
      )}
    </span>
  );
}

const RECOMMENDATION_CLASS: Record<RecommendationId, string> = {
  strong: "bg-fit-5 text-fit-5-foreground",
  apply: "bg-fit-3 text-fit-3-foreground",
  maybe: "bg-secondary text-secondary-foreground",
  skip: "bg-muted text-muted-foreground",
};

export function RecommendationPill({
  value,
  className,
}: {
  value: RecommendationId | string | null | undefined;
  className?: string;
}) {
  if (!value || !(value in RECOMMENDATION_LABEL)) return null;
  const id = value as RecommendationId;
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-full px-2 text-[11.5px] font-medium whitespace-nowrap",
        RECOMMENDATION_CLASS[id],
        className,
      )}
    >
      {RECOMMENDATION_LABEL[id]}
    </span>
  );
}

/**
 * A company monogram. No logos are fetched — that would tell a third party
 * which companies this user is looking at — so each company gets initials on
 * a hue derived from its name, stable across the app.
 */
export function CompanyMark({
  name,
  size = "md",
  className,
}: {
  name: string | null | undefined;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const hue = nameHue(name);
  const dims = {
    sm: "size-6 rounded-md text-[10px]",
    md: "size-9 rounded-lg text-[12.5px]",
    lg: "size-14 rounded-xl text-[18px]",
  }[size];

  return (
    <span
      aria-hidden
      className={cn(
        "text-display inline-flex shrink-0 items-center justify-center font-semibold tracking-[-0.01em] select-none",
        dims,
        className,
      )}
      style={{
        background: `oklch(0.94 0.035 ${hue})`,
        color: `oklch(0.36 0.07 ${hue})`,
      }}
    >
      {initials(name)}
    </span>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0 space-y-1.5">
        <h1 className="text-display text-[28px] leading-[1.1] sm:text-[32px]">
          {title}
        </h1>
        {description && (
          <p className="text-muted-foreground max-w-[62ch] text-[14.5px] leading-relaxed">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

/** The standard page body: generous gutters, a readable maximum width. */
export function PageBody({
  children,
  className,
  width = "wide",
}: {
  children: React.ReactNode;
  className?: string;
  width?: "narrow" | "wide" | "full";
}) {
  return (
    <div
      className={cn(
        "mx-auto w-full px-5 py-7 sm:px-8 sm:py-9 lg:px-10",
        width === "narrow" && "max-w-3xl",
        width === "wide" && "max-w-6xl",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** A titled region inside a page. Headings are sentence case, never shouted. */
export function Section({
  title,
  description,
  action,
  children,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      <div className="flex items-baseline justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-display text-[17px] leading-tight">{title}</h2>
          {description && (
            <p className="text-muted-foreground mt-0.5 text-[13px]">{description}</p>
          )}
        </div>
        {action && <div className="shrink-0 text-[13px]">{action}</div>}
      </div>
      {children}
    </section>
  );
}

/**
 * An empty state is an invitation to act: a short statement of what would be
 * here, and the one thing to do about it.
 */
export function EmptyState({
  title,
  children,
  action,
  className,
}: {
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-3 rounded-xl border border-dashed px-6 py-8",
        className,
      )}
    >
      <div className="space-y-1">
        <p className="text-display text-[17px]">{title}</p>
        {children && (
          <div className="text-muted-foreground max-w-[56ch] text-[13.5px] leading-relaxed">
            {children}
          </div>
        )}
      </div>
      {action}
    </div>
  );
}

/** A number with its label beneath — used sparingly, where counts matter. */
export function Stat({
  value,
  label,
  className,
}: {
  value: React.ReactNode;
  label: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="text-display tabular text-[26px] leading-none">{value}</div>
      <div className="text-muted-foreground mt-1.5 text-[12.5px]">{label}</div>
    </div>
  );
}

/** A signal dot: marigold and pulsing while something is live. */
export function SignalDot({
  tone = "signal",
  pulse = false,
  className,
}: {
  tone?: "signal" | "ok" | "error" | "idle";
  pulse?: boolean;
  className?: string;
}) {
  const color = {
    signal: "bg-signal",
    ok: "bg-success",
    error: "bg-destructive",
    idle: "bg-muted-foreground/50",
  }[tone];
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        color,
        pulse && "animate-signal",
        className,
      )}
    />
  );
}
