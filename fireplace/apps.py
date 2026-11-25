from django.apps import AppConfig


class FireplaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fireplace"

    def ready(self):
        from . import worker

        if worker.should_start():
            worker.start()
