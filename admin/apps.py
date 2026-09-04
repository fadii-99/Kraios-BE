from django.apps import AppConfig


class KraiosAdminConfig(AppConfig):
    """
    Application config for the KRAIOS admin console API.

    The package is called ``admin`` because that is what the console is, but
    Django's own ``django.contrib.admin`` already occupies the app *label*
    ``admin`` and two apps may not share one. The label is therefore overridden
    to ``kraios_admin``; every migration, table name and ``app_label`` reference
    in this app uses that name, while imports stay ``admin.<module>``.

    Do not "simplify" this by dropping ``label`` — the app registry raises
    ``ImproperlyConfigured: Application labels aren't unique`` at startup.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'admin'
    label = 'kraios_admin'
    verbose_name = 'KRAIOS Admin Console'

    def ready(self):
        # Registers the deploy-time configuration checks. Imported here rather
        # than at module scope because the check registry is not ready until
        # the app registry is.
        from . import checks  # noqa: F401
