from django.apps import AppConfig


class BimConfig(AppConfig):
    """The BIM engine app.

    Self-contained on purpose: nothing outside this package imports from it, so
    removing `'bim'` from INSTALLED_APPS and deleting the directory takes the
    whole feature with it. See bim/README.md for the removal steps.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'bim'
    verbose_name = 'BIM engine'
