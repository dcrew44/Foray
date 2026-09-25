import { PageBody } from "@/components/foray/primitives";

/**
 * Shown the instant a navigation starts, while the server renders the page.
 * Shapes only — a spinner would say "wait" without saying what for.
 */
export default function Loading() {
  return (
    <PageBody>
      <div aria-busy="true" aria-label="Loading" className="animate-pulse space-y-10">
        <div className="space-y-3">
          <div className="bg-muted h-9 w-72 rounded-lg" />
          <div className="bg-muted/70 h-4 w-96 max-w-full rounded" />
        </div>
        <div className="space-y-3">
          {Array.from({ length: 5 }, (_, index) => (
            <div className="flex items-center gap-3.5" key={index}>
              <div className="bg-muted size-9 shrink-0 rounded-lg" />
              <div className="flex-1 space-y-2">
                <div className="bg-muted h-3.5 w-2/3 rounded" />
                <div className="bg-muted/70 h-3 w-1/3 rounded" />
              </div>
            </div>
          ))}
        </div>
      </div>
    </PageBody>
  );
}
