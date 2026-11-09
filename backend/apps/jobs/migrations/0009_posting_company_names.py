"""Postings that carry their board's slug as the company name take the company's own.

Ashby, Lever and Gem say nothing of who the employer is, so their adapters named every posting
for its board (`notion`, `cerebras`), and that is what match rows and notifications showed.
`ingest._name_for_the_company` now gives new and changed postings the registry's name; this
does the same for the rows already in the mirror, with the dedupe key that is made from it.
"""

import json

from django.db import migrations

BATCH = 500


def rename(apps, schema_editor):
    from apps.jobs.sources.dedupe import dedupe_key

    Job = apps.get_model("jobs", "Job")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT j.id, c.name, j.title, j.location_norm
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE lower(j.company_name) IN (lower(coalesce(c.ats_slug, '')), lower(c.slug))
              AND j.company_name <> c.name AND c.name <> ''
            """
        )
        rows = cursor.fetchall()
    for start in range(0, len(rows), BATCH):
        batch = []
        for job_id, name, title, place in rows[start : start + BATCH]:
            # A raw cursor hands a JSON column back as text.
            place = json.loads(place) if isinstance(place, str) else place
            place = place if isinstance(place, dict) else {}
            batch.append(
                Job(
                    id=job_id,
                    company_name=name,
                    dedupe_key=dedupe_key(name, title, place.get("city") or place.get("raw")),
                )
            )
        Job.objects.bulk_update(batch, ["company_name", "dedupe_key"])


class Migration(migrations.Migration):
    dependencies = [("jobs", "0008_company_provenance")]

    operations = [migrations.RunPython(rename, migrations.RunPython.noop)]
