import os
import sys
import unittest
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test.utils import setup_test_environment, teardown_test_environment
from apps.accounts.models import Client

def main():
    print("=== Running Product Image Integration Test Suite ===")
    setup_test_environment()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    from apps.masters.tests_product_image import ProductImageIntegrationTests
    suite.addTests(loader.loadTestsFromTestCase(ProductImageIntegrationTests))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    teardown_test_environment()

    # Cleanup test tenant
    Client.objects.filter(slug="image-test-tenant").delete()
    print("Test cleanup completed.")

    if not result.wasSuccessful():
        sys.exit(1)
    print("=== Product Image Tests Passed Successfully! ===")

if __name__ == "__main__":
    main()
