from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from apps.core.validators import (
    validate_email_format,
    validate_phone_number,
    validate_pincode_format,
    validate_address_dict,
)


class CoreValidatorsTests(SimpleTestCase):
    def test_email_validation_valid(self):
        self.assertEqual(validate_email_format("test@example.com"), "test@example.com")
        self.assertEqual(validate_email_format("  USER@DOMAIN.CO.IN  "), "user@domain.co.in")
        self.assertEqual(validate_email_format(""), "")
        self.assertEqual(validate_email_format(None), None)

    def test_email_validation_invalid(self):
        with self.assertRaises(ValidationError):
            validate_email_format("invalid-email")
        with self.assertRaises(ValidationError):
            validate_email_format("test@")
        with self.assertRaises(ValidationError):
            validate_email_format("@domain.com")
        with self.assertRaises(ValidationError):
            validate_email_format("", required=True)

    def test_phone_validation_valid(self):
        self.assertEqual(validate_phone_number("9876543210"), "9876543210")
        self.assertEqual(validate_phone_number("+91 99988 67024"), "+91 99988 67024")
        self.assertEqual(validate_phone_number("+1-555-0199123"), "+1-555-0199123")
        self.assertEqual(validate_phone_number("(022) 2850-1234"), "(022) 2850-1234")
        self.assertEqual(validate_phone_number(""), "")
        self.assertEqual(validate_phone_number(None), None)
        self.assertEqual(validate_phone_number("+91"), "")

    def test_phone_validation_invalid(self):
        with self.assertRaises(ValidationError):
            validate_phone_number("123")  # too short (< 10 digits)
        with self.assertRaises(ValidationError):
            validate_phone_number("abcdefghij")  # non-digits
        with self.assertRaises(ValidationError):
            validate_phone_number("98765432101234567")  # too long (> 15 digits)
        with self.assertRaises(ValidationError):
            validate_phone_number("98765@#1234")  # invalid symbols

    def test_pincode_validation_valid(self):
        self.assertEqual(validate_pincode_format("560100"), "560100")
        self.assertEqual(validate_pincode_format("394230"), "394230")
        self.assertEqual(validate_pincode_format("  382445  "), "382445")
        self.assertEqual(validate_pincode_format(""), "")
        self.assertEqual(validate_pincode_format(None), None)

    def test_pincode_validation_invalid(self):
        with self.assertRaises(ValidationError):
            validate_pincode_format("012345")  # starts with 0
        with self.assertRaises(ValidationError):
            validate_pincode_format("12345")  # only 5 digits
        with self.assertRaises(ValidationError):
            validate_pincode_format("1234567")  # 7 digits
        with self.assertRaises(ValidationError):
            validate_pincode_format("56010A")  # contains letter
        with self.assertRaises(ValidationError):
            validate_pincode_format("", required=True)

    def test_address_dict_validation(self):
        valid_addr = {"line1": "123 Main St", "city": "Surat", "pincode": "394230"}
        self.assertEqual(validate_address_dict(valid_addr), valid_addr)

        invalid_addr = {"line1": "123 Main St", "city": "Surat", "pincode": "999"}
        with self.assertRaises(ValidationError):
            validate_address_dict(invalid_addr)
