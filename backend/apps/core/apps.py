from typing import Any

from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import pre_migrate


def _refuse_legacy_target(sender: AppConfig, using: str = "default", **kwargs: Any) -> None:
    """`migrate` emits pre_migrate before its recorder creates `django_migrations`, so a
    legacy-looking target stops here, before anything is written (§1.3, §3.1)."""
    from apps.core.guards import assert_app_database

    assert_app_database(connections[using])


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        pre_migrate.connect(_refuse_legacy_target, sender=self, dispatch_uid="foray-legacy-guard")
