import re
from django.core.validators import validate_email as django_validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

# Regex for allowed phone characters: optional leading +, digits, spaces, hyphens, and parentheses
PHONE_ALLOWED_CHARS_RE = re.compile(r"^\+?[0-9\s\-()]+$")

# Standard 6-digit Indian postal PIN code (cannot start with 0)
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")


def validate_email_format(value, field_name="email", required=False):
    """Validates that a string is a properly formatted RFC-compliant email address.
    Normalizes by stripping whitespace and converting to lowercase.
    """
    if value is None:
        if required:
            raise serializers.ValidationError({field_name: "Email address is required."})
        return value

    clean = str(value).strip()
    if not clean:
        if required:
            raise serializers.ValidationError({field_name: "Email address is required."})
        return ""

    try:
        django_validate_email(clean)
    except DjangoValidationError:
        raise serializers.ValidationError(
            {field_name: "Please enter a valid email address (e.g. name@example.com)."}
        )

    return clean.lower()


def validate_phone_number(value, field_name="phone", required=False):
    """Validates that a phone number contains valid characters and between 10 and 15 digits.
    Accommodates Indian 10-digit mobiles, numbers with +91 or country codes, and international formats.
    """
    if value is None:
        if required:
            raise serializers.ValidationError({field_name: "Phone number is required."})
        return value

    clean = str(value).strip()
    if not clean or clean == "+91":
        if required:
            raise serializers.ValidationError({field_name: "Phone number is required."})
        return ""

    if not PHONE_ALLOWED_CHARS_RE.match(clean):
        raise serializers.ValidationError(
            {field_name: "Phone number can only contain digits, spaces, hyphens, and an optional leading '+'."}
        )

    digits = re.sub(r"\D", "", clean)
    if len(digits) < 10 or len(digits) > 15:
        raise serializers.ValidationError(
            {field_name: "Phone number must contain between 10 and 15 digits."}
        )

    return clean


def validate_pincode_format(value, field_name="pincode", required=False):
    """Validates that a postal pincode is a 6-digit numeric string (e.g. 560100)."""
    if value is None:
        if required:
            raise serializers.ValidationError({field_name: "Pincode is required."})
        return value

    clean = str(value).strip()
    if not clean:
        if required:
            raise serializers.ValidationError({field_name: "Pincode is required."})
        return ""

    if not PINCODE_RE.match(clean):
        raise serializers.ValidationError(
            {field_name: "Pincode must be a 6-digit number (e.g. 560100)."}
        )

    return clean


def validate_address_dict(address, field_name="address", required_pincode=False):
    """Inspects an address dictionary and validates any embedded pincode / pin_code / postal_code."""
    if not isinstance(address, dict):
        return address

    pincode = address.get("pincode") or address.get("pin_code") or address.get("postal_code")
    if pincode or required_pincode:
        validate_pincode_format(pincode, field_name=f"{field_name}.pincode", required=required_pincode)

    return address
