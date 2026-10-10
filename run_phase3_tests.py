import os
import sys
import unittest
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test.utils import setup_test_environment, teardown_test_environment
from apps.accounts.models import Client

def main():
    print("=== Running Phase 2 & Phase 3 Test Suite ===")
    setup_test_environment()

    # Load test cases
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    from apps.masters.tests_phase3_metal_items import Phase3MetalItemMasterTests
    from apps.masters.tests_item_types_categories import ItemTypeAndCategoryMasterTests

    suite.addTests(loader.loadTestsFromTestCase(Phase3MetalItemMasterTests))
    suite.addTests(loader.loadTestsFromTestCase(ItemTypeAndCategoryMasterTests))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    teardown_test_environment()

    # Cleanup test tenants
    Client.objects.filter(slug__in=["test-phase2-tenant", "test-phase3-metal-tenant"]).delete()
    print("Test cleanup completed.")

    if not result.wasSuccessful():
        sys.exit(1)
    print("=== All Tests Passed Successfully! ===")

if __name__ == "__main__":
    main()
