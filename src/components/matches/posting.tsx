"use client";

import { ChevronDown } from "lucide-react";
import { useState } from "react";

/**
 * A posting's cleaned text, as paragraphs and bullets. Long ones start
 * collapsed — the evaluation above is usually what the user came for.
 */

const COLLAPSED_CHARS = 1600;

type Block = { kind: "p"; text: string } | { kind: "ul"; items: string[] };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const chunk of text.split(/\n\s*\n/)) {
    const lines = chunk.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length === 0) continue;
    const bullets = lines.filter((line) => /^([•·▪◦*-]|\d+[.)])\s+/.test(line));
    if (bullets.length >= Math.max(2, lines.length - 1)) {
      const lead = lines.filter((line) => !bullets.includes(line));
      if (lead.length) out.push({ kind: "p", text: lead.join(" ") });
      out.push({ kind: "ul", items: bullets.map((line) => line.replace(/^([•·▪◦*-]|\d+[.)])\s+/, "")) });
    } else {
      out.push({ kind: "p", text: lines.join("\n") });
    }
  }
  return out;
}

export function Posting({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false);

  if (!text?.trim()) {
    return (
      <p className="text-muted-foreground text-[13.5px]">
        The source did not include a description. Open the posting to read it.
      </p>
    );
  }

  const long = text.length > COLLAPSED_CHARS;
  const shown = long && !open ? text.slice(0, COLLAPSED_CHARS).replace(/\s+\S*$/, "") + "…" : text;

  return (
    <div className="relative">
      <div className="prose-foray text-foreground/90 max-w-[72ch] space-y-3 text-[14.5px]">
        {blocks(shown).map((block, index) =>
          block.kind === "p" ? (
            <p className="whitespace-pre-line" key={index}>
              {block.text}
            </p>
          ) : (
            <ul key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{item}</li>
              ))}
            </ul>
          ),
        )}
      </div>
      {long && (
        <button
          className="text-primary mt-4 inline-flex items-center gap-1 text-[13.5px] font-medium"
          onClick={() => setOpen(!open)}
          type="button"
        >
          <ChevronDown className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} />
          {open ? "Show less" : "Read the full posting"}
        </button>
      )}
    </div>
  );
}
