"""
First-password activation for accounts created without one.

There is no default password: an account created without a password gets an
unusable one (``check_password`` is always False), and its owner is emailed a
link to the sign-in page's "Forgot password" flow. That flow's one-time code is
the activation token -- hashed, 15-minute, attempt-limited and bound to the
address -- so a second token type would only duplicate it.
"""
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction


def activation_url(email):
    query = urlencode({"action": "forgot-password", "email": email})
    return f"{settings.FRONTEND_URL}/login?{query}"


def send_invite_on_commit(user):
    """Email ``user`` how to set a password, once the creating transaction commits."""
    from apps.core.emails import send_account_invite_email

    if user.has_usable_password() or not user.email:
        return
    email = user.email
    name = user.name or ""
    workspace = getattr(user.client, "name", "") if user.client_id else ""
    transaction.on_commit(
        lambda: send_account_invite_email(
            email=email,
            user_name=name,
            workspace=workspace,
            activate_url=activation_url(email),
        )
    )
