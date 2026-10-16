import logging

from celery import shared_task
from rest_framework.exceptions import APIException

from payments.models import Payment
from payments.services import confirm_session, retrieve_session


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def sync_payment_sessions():
    checked = 0
    failed = 0
    sessions = Payment.objects.filter(
        status=Payment.Status.PENDING,
        session_id__isnull=False
    ).values_list("pk", "session_id")
    for payment_id, session_id in sessions.iterator(chunk_size=100):
        try:
            confirm_session(retrieve_session(session_id))
            checked += 1

        except APIException:
            failed += 1
            logger.warning("Could not synchronize payment %s", payment_id)

    return {"checked": checked, "failed": failed}
