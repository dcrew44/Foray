CREATE TABLE "api_usage" (
	"id" text PRIMARY KEY NOT NULL,
	"provider" text NOT NULL,
	"period" text NOT NULL,
	"requests" integer DEFAULT 0 NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE INDEX "api_usage_provider_period_idx" ON "api_usage" USING btree ("provider","period");