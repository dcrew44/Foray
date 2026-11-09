import {
  clearanceLabel,
  levelLabel,
  workAuthLabel,
  yearsLabel,
  type JobFacts,
} from "@/lib/job-facts";

/**
 * What the posting asks of whoever applies: its level, the experience it requires, and what it
 * says about work authorization and clearances, each with the posting's own sentence. These are
 * the same facts a junior search filters on, so a posting that should not be here shows why it
 * got through.
 */
export function JobFactsList({ facts }: { facts: JobFacts }) {
  if (!facts.read) {
    return (
      <p className="text-muted-foreground text-[13px] leading-relaxed">
        Not read yet. Foray reads what a posting asks for when it next checks the board.
      </p>
    );
  }
  return (
    <dl className="text-[13px]">
      <Fact label="Level" value={levelLabel(facts)} />
      <Fact evidence={facts.yearsEvidence} label="Experience" value={yearsLabel(facts)} />
      <Fact
        evidence={facts.workAuthEvidence}
        label="Work authorization"
        value={workAuthLabel(facts)}
      />
      <Fact evidence={facts.clearanceEvidence} label="Clearance" value={clearanceLabel(facts)} />
    </dl>
  );
}

function Fact({
  label,
  value,
  evidence,
}: {
  label: string;
  value: string;
  evidence?: string | null;
}) {
  return (
    <div className="border-b py-2.5 last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 font-medium">{value}</dd>
      {evidence && (
        <dd className="text-muted-foreground mt-1 text-[12.5px] leading-snug">
          <q>{evidence}</q>
        </dd>
      )}
    </div>
  );
}
