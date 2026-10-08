# The app database bounds a leaked transaction at 60 s (design §2.2). The statement names no
# database: it alters whichever one is being migrated (`current_database()`), so pytest's
# `test_foray_app` and the rehearsal's `foray_rehearsal` alter themselves, never the dev
# database. The setting applies to sessions opened afterwards. The importer turns it off for its
# own sessions (§3.2).

from django.db import migrations

SET = """
DO $$ BEGIN
  EXECUTE format('ALTER DATABASE %I SET idle_in_transaction_session_timeout = %L',
                 current_database(), '60s');
END $$;
"""

RESET = """
DO $$ BEGIN
  EXECUTE format('ALTER DATABASE %I RESET idle_in_transaction_session_timeout',
                 current_database());
END $$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(SET, reverse_sql=RESET),
    ]
