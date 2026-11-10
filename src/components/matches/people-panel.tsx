import { ExternalLink } from "lucide-react";
import Link from "next/link";

import { Section } from "@/components/foray/primitives";
import type { components } from "@/lib/api/schema";
import { plural } from "@/lib/format";

type MatchPeople = components["schemas"]["MatchPeople"];

/**
 * The people the user knows at this posting's company (#64), from the contacts they imported:
 * who they are, what they do there, and their profile when the file gave one. A posting where
 * the user knows nobody has no such section.
 *
 * Foray contacts nobody. This says who could be asked; asking is the user's to do.
 */
export function PeoplePanel({ knows, company }: { knows: MatchPeople; company: string }) {
  if (knows.count === 0) return null;
  const more = knows.count - knows.people.length;

  return (
    <Section
      description={`${plural(knows.count, "contact")} of yours ${knows.count === 1 ? "works" : "work"} at ${company}.`}
      title="People you know here"
    >
      <ul className="divide-y rounded-xl border text-[13px]">
        {knows.people.map((person) => (
          <li className="px-3.5 py-2.5" key={person.id}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 truncate font-medium">{person.name}</span>
              {person.profileUrl && (
                <a
                  className="text-muted-foreground hover:text-foreground inline-flex shrink-0 items-center gap-1 text-[12.5px]"
                  href={person.profileUrl}
                  rel="noreferrer"
                  target="_blank"
                >
                  Profile
                  <ExternalLink className="size-3" />
                </a>
              )}
            </div>
            {person.position && (
              <p className="text-muted-foreground mt-0.5 leading-snug">{person.position}</p>
            )}
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground text-[12.5px] leading-snug">
        {more > 0 && (
          <>
            And {more.toLocaleString()} more on the{" "}
            <Link className="underline" href={`/contacts?q=${encodeURIComponent(company)}`}>
              Contacts page
            </Link>
            .{" "}
          </>
        )}
        A referral asked for while a posting is new counts for more. Foray contacts nobody:
        reaching out is yours to do.
      </p>
    </Section>
  );
}
