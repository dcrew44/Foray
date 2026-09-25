import Link from "next/link";

import { PageBody } from "@/components/foray/primitives";
import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <PageBody width="narrow">
      <div className="space-y-4 py-10">
        <h1 className="text-display text-[28px]">Nothing here.</h1>
        <p className="text-muted-foreground max-w-[56ch] text-[14.5px] leading-relaxed">
          That match, chat or page does not exist, or it belongs to another account.
        </p>
        <div className="flex gap-2">
          <Link className={buttonVariants()} href="/matches">
            Your matches
          </Link>
          <Link className={buttonVariants({ variant: "outline" })} href="/home">
            Home
          </Link>
        </div>
      </div>
    </PageBody>
  );
}
