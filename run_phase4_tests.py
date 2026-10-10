import os
import sys
import unittest
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test.utils import setup_test_environment, teardown_test_environment
from apps.accounts.models import Client

def main():
    print("=== Running Phase 4 Stock Ledger & Balance Test Suite ===")
    setup_test_environment()

    # Load test cases
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    from apps.inventory.tests_phase4_stock_ledger import Phase4StockLedgerTests
    suite.addTests(loader.loadTestsFromTestCase(Phase4StockLedgerTests))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    teardown_test_environment()

    # Cleanup test tenant and dependencies
    from apps.accounting.models import Account
    from apps.masters.models import Item, ItemCategory, ItemType, MaterialGrade, Location, Unit
    from apps.accounts.models import User
    Account.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    Item.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    ItemCategory.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    ItemType.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    MaterialGrade.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    Location.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    Unit.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    User.objects.filter(client__slug="test-phase4-ledger-tenant").delete()
    Client.objects.filter(slug="test-phase4-ledger-tenant").delete()
    print("Test cleanup completed.")

    if not result.wasSuccessful():
        sys.exit(1)
    print("=== All Phase 4 Tests Passed Successfully! ===")

if __name__ == "__main__":
    main()
