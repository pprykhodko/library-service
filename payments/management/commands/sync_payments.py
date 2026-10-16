from django.core.management.base import BaseCommand, CommandError

from payments.tasks import sync_payment_sessions


class Command(BaseCommand):
    help = "Synchronize pending Stripe sessions once without a Celery worker."

    def handle(self, *args, **options):
        result = sync_payment_sessions()
        self.stdout.write(f"Checked: {result['checked']}; failed: {result['failed']}")

        if result["failed"]:
            raise CommandError("Some payments could not be synchronized; retry later.")
