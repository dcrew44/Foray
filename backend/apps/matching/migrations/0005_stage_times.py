"""When a match first reached Applied and Interviewing (#94).

`job_matches` kept only where a match is now, so the Outcomes page could not count an interview
that ended in a rejection, or an application made by hand that has since closed. These two
columns keep the first time each step was reached.

Existing rows have no history to recover. What is known is filled in: a match in Applied got
there at `stage_changed_at`, and a match in Interviewing or Offer reached Interviewing by then
(when it was applied to is not known, and stays empty). A match already Rejected or Archived
keeps both empty and is counted as before.
"""

from django.db import migrations, models

BACKFILL = """
    UPDATE job_matches SET applied_at = stage_changed_at WHERE stage = 'applied';
    UPDATE job_matches SET interviewing_at = stage_changed_at
    WHERE stage IN ('interviewing', 'offer');
"""


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0004_match_lane_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobmatch",
            name="applied_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="jobmatch",
            name="interviewing_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
    ]
