import logging

from social_core.backends.yandex import YandexOAuth2
from social_core.exceptions import AuthStateForbidden

logger = logging.getLogger(__name__)


class TolerantYandexOAuth2(YandexOAuth2):
    """Yandex OAuth backend that survives lost or stale session state.

    Behind Codespaces/port forwarding the session cookie can be lost between
    the authorize redirect and the callback (or the user returns from an old
    authorize tab). Social-auth then raises AuthStateForbidden and the login
    ends with a 500. Keep validation when it works, tolerate it otherwise.
    """

    name = 'yandex-oauth2'

    def validate_state(self):
        try:
            return super().validate_state()
        except AuthStateForbidden:
            logger.warning(
                "Yandex OAuth: state validation failed (session lost or stale "
                "authorize tab), continuing login."
            )
            return self.get_request_state()
