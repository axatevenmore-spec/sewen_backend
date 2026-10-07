"""Email utilities for Evenmore ERP."""
import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def send_password_reset_otp_email(email: str, otp: str, user_name: str = "", expiry_minutes: int = 15) -> bool:
    """Send a 6-digit OTP email to the user for password reset."""
    subject = f"{otp} is your Evenmore ERP password reset verification code"
    greeting = f"Hello {user_name}," if user_name else "Hello,"

    plain_text = (
        f"{greeting}\n\n"
        f"You requested to reset your password for your Evenmore ERP account.\n\n"
        f"Your 6-digit verification code is:\n\n"
        f"    {otp}\n\n"
        f"This code will expire in {expiry_minutes} minutes.\n\n"
        f"If you did not request a password reset, please ignore this email or contact your workspace administrator immediately.\n\n"
        f"— The Evenmore ERP Team\n"
    )

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Password Reset Code</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #f8fafc;
      margin: 0;
      padding: 0;
      color: #1e293b;
    }}
    .container {{
      max-width: 520px;
      margin: 40px auto;
      background-color: #ffffff;
      border-radius: 16px;
      border: 1px solid #e2e8f0;
      overflow: hidden;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    }}
    .header {{
      background: linear-gradient(135deg, #2563eb, #4f46e5);
      padding: 32px 28px;
      text-align: center;
      color: #ffffff;
    }}
    .header h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: -0.5px;
    }}
    .header p {{
      margin: 6px 0 0;
      font-size: 13px;
      opacity: 0.9;
    }}
    .content {{
      padding: 32px 28px;
    }}
    .otp-box {{
      background: #eff6ff;
      border: 2px dashed #93c5fd;
      border-radius: 12px;
      padding: 20px;
      text-align: center;
      margin: 24px 0;
    }}
    .otp-code {{
      font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, Courier, monospace;
      font-size: 36px;
      font-weight: 800;
      letter-spacing: 8px;
      color: #1d4ed8;
      display: inline-block;
      margin: 0;
    }}
    .otp-expiry {{
      margin-top: 8px;
      font-size: 12px;
      color: #64748b;
      font-weight: 500;
    }}
    .security-note {{
      background-color: #fffbeb;
      border-left: 4px solid #f59e0b;
      padding: 12px 16px;
      border-radius: 6px;
      font-size: 12px;
      color: #92400e;
      line-height: 1.5;
      margin: 20px 0;
    }}
    .footer {{
      padding: 20px 28px;
      background-color: #f8fafc;
      border-top: 1px solid #e2e8f0;
      text-align: center;
      font-size: 11px;
      color: #94a3b8;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Evenmore ERP</h1>
      <p>Security &amp; Account Verification</p>
    </div>
    <div class="content">
      <p style="font-size: 15px; font-weight: 600; margin-top: 0;">{greeting}</p>
      <p style="font-size: 14px; color: #475569; line-height: 1.6;">
        We received a request to reset the password for your account associated with <strong>{email}</strong>. Use the 6-digit verification code below to complete the reset process:
      </p>
      <div class="otp-box">
        <div class="otp-code">{otp}</div>
        <div class="otp-expiry">Valid for {expiry_minutes} minutes</div>
      </div>
      <div class="security-note">
        <strong>Security Alert:</strong> Never share this verification code with anyone. Evenmore Support will never ask for your code.
      </div>
      <p style="font-size: 13px; color: #64748b; line-height: 1.5;">
        If you did not initiate this request, you can safely ignore this email. Your current password will remain unchanged.
      </p>
    </div>
    <div class="footer">
      &copy; Evenmore Infotech ERP Suite &bull; Automated System Notification
    </div>
  </div>
</body>
</html>
"""

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Evenmore ERP <noreply@evenmore.io>")
    msg = EmailMultiAlternatives(
        subject=subject,
        body=plain_text,
        from_email=from_email,
        to=[email],
    )
    msg.attach_alternative(html_content, "text/html")

    try:
        msg.send(fail_silently=False)
        logger.info("Sent password reset OTP email to %s", email)
        return True
    except Exception as exc:
        logger.exception("Failed to send password reset OTP email to %s: %s", email, exc)
        return False


def send_account_invite_email(email: str, user_name: str = "", workspace: str = "", activate_url: str = "") -> bool:
    """Tell a newly created user how to set their first password.

    The account is created without a usable password; the link opens the
    sign-in page's "Forgot password" flow, where an emailed one-time code
    proves the address before any password is set.
    """
    from html import escape

    subject = "Your Evenmore ERP account is ready - set your password"
    greeting = f"Hello {user_name}," if user_name else "Hello,"
    where = f" for {workspace}" if workspace else ""

    plain_text = (
        f"{greeting}\n\n"
        f"An Evenmore ERP account{where} has been created for {email}.\n\n"
        f"To activate it, open the link below, request a verification code and choose your password:\n\n"
        f"    {activate_url}\n\n"
        f"If you were not expecting this, you can ignore this email.\n\n"
        f"— The Evenmore ERP Team\n"
    )
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b;">
  <div style="max-width: 520px; margin: 40px auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 32px 28px;">
    <p style="font-size: 15px; font-weight: 600; margin-top: 0;">{escape(greeting)}</p>
    <p style="font-size: 14px; color: #475569; line-height: 1.6;">
      An Evenmore ERP account{escape(where)} has been created for <strong>{escape(email)}</strong>.
      To activate it, request a verification code and choose your password.
    </p>
    <p style="text-align: center; margin: 28px 0;">
      <a href="{escape(activate_url, quote=True)}" style="background: #2563eb; color: #ffffff; padding: 12px 22px; border-radius: 10px; text-decoration: none; font-weight: 600;">Set your password</a>
    </p>
    <p style="font-size: 12px; color: #64748b;">If you were not expecting this, you can ignore this email.</p>
  </div>
</body>
</html>
"""
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Evenmore ERP <noreply@evenmore.io>")
    msg = EmailMultiAlternatives(subject=subject, body=plain_text, from_email=from_email, to=[email])
    msg.attach_alternative(html_content, "text/html")
    try:
        msg.send(fail_silently=False)
        logger.info("Sent account invite email to %s", email)
        return True
    except Exception as exc:
        logger.exception("Failed to send account invite email to %s: %s", email, exc)
        return False
