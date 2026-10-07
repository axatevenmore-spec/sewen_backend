"""
Django-level error handlers (config/urls.py ``handler400`` ... ``handler500``).

DRF views already answer through ``api_exception_handler``; these cover the
rest -- an unknown URL, a failure in middleware -- so a client never receives
Django's HTML error page, and never a traceback.
"""
from django.http import JsonResponse

from .exceptions import Codes


def _error(message, code, status):
    return JsonResponse({"message": message, "code": code}, status=status)


def bad_request(request, exception=None):
    return _error("The request could not be understood.", Codes.VALIDATION_FAILED, 400)


def permission_denied(request, exception=None):
    return _error("You do not have permission to perform this action.", Codes.PERMISSION_DENIED, 403)


def not_found(request, exception=None):
    return _error("That resource does not exist.", Codes.NOT_FOUND, 404)


def server_error(request):
    return _error("The server is unavailable. Please try again shortly.", "INTERNAL_ERROR", 500)
