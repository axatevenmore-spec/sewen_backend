import io
from datetime import date
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles
from apps.accounts.authentication import build_tokens
from apps.core import files as file_service
from apps.core.models import File
from apps.masters.models import Item, ItemCategory, ItemType, MaterialGrade, Party
from apps.sales.models import SalesOrder, SalesOrderLine
from apps.sales.portal import orderable_items


class ProductImageIntegrationTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name='Image Test Tenant', slug='image-test-tenant')
        seed_roles(self.tenant)
        self.admin_role = Role.objects.get(client=self.tenant, code='AD')
        self.customer_role = Role.objects.get(client=self.tenant, code='CU')
        self.user = User.objects.create_superuser(
            client=self.tenant,
            email='admin@imagetest.com',
            password='Password123!',
            role=self.admin_role,
        )
        self.tokens = build_tokens(self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens['access']}")

        self.item_type = ItemType.objects.create(client=self.tenant, name='Test Type', code='TT')
        self.category = ItemCategory.objects.create(
            client=self.tenant, name='Test Category', code='TC', item_type=self.item_type
        )
        self.grade = MaterialGrade.objects.create(client=self.tenant, name='MS', code='MS')

        self.customer = Party.objects.create(
            client=self.tenant,
            code='CUST-001',
            name='Test Customer',
            type='Customer',
            status='Active',
        )
        self.customer_user = User.objects.create_user(
            client=self.tenant,
            email='cust@imagetest.com',
            password='Password123!',
            party=self.customer,
            role=self.customer_role,
        )

    def test_upload_product_image_and_create_item(self):
        init_res = self.api.post(
            '/api/v1/files/upload-url/',
            {
                'fileName': 'test_gear.png',
                'contentType': 'image/png',
                'size': 128,
                'scope': 'product_image',
            },
            format='json',
        )
        self.assertEqual(init_res.status_code, 201)
        file_id = init_res.data['fileId']
        upload_url = init_res.data['uploadUrl']

        put_token = upload_url.split('token=')[-1]
        file_obj = io.BytesIO(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00'
        )
        file_obj.name = 'test_gear.png'
        put_res = self.api.put(
            f'/api/v1/files/{file_id}/upload/?token={put_token}',
            data={'file': file_obj},
            format='multipart',
        )
        self.assertEqual(put_res.status_code, 204)

        commit_res = self.api.post(f'/api/v1/files/{file_id}/commit/', {}, format='json')
        self.assertEqual(commit_res.status_code, 200)
        self.assertIn('url', commit_res.data)

        item_res = self.api.post(
            '/api/v1/inventory/items/',
            {
                'sku': 'SKU-IMG-001',
                'name': 'Industrial Gear Assembly',
                'itemKind': 'Standalone',
                'categoryId': str(self.category.id),
                'itemTypeId': str(self.item_type.id),
                'gradeId': str(self.grade.id),
                'uom': 'Nos',
                'sellingPrice': 450.0,
                'costPrice': 250.0,
                'imageFileId': file_id,
            },
            format='json',
        )
        self.assertEqual(item_res.status_code, 201)
        created = item_res.data
        self.assertIsNotNone(created.get('image'))
        self.assertIsNotNone(created.get('imageUrl'))
        self.assertIn(f'/files/{file_id}/download/', created['imageUrl'])
        self.assertEqual(created.get('imageFileId'), file_id)

        item = Item.objects.get(pk=created['id'])
        self.assertEqual(str(item.image_file_id), file_id)

        dl_res = self.api.get(created['imageUrl'])
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res['Content-Type'], 'image/png')
        self.assertNotIn('attachment', dl_res.get('Content-Disposition', ''))

    def test_update_and_remove_product_image(self):
        item = Item.objects.create(
            client=self.tenant,
            sku='SKU-UPDATE-01',
            name='Shaft Rod',
            category=self.category,
            selling_price=100,
        )

        file_row = File.objects.create(
            client=self.tenant,
            scope='product_image',
            storage_key=file_service.build_storage_key(self.tenant.id, 'product_image', 'shaft.png'),
            file_name='shaft.png',
            content_type='image/png',
            file_size=64,
            status='committed',
        )
        file_service.write_bytes(file_row.storage_key, b'fake png bytes')

        patch_res = self.api.patch(
            f'/api/v1/inventory/items/{item.id}/',
            {'imageFileId': str(file_row.id)},
            format='json',
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertIsNotNone(patch_res.data.get('imageUrl'))

        item.refresh_from_db()
        self.assertEqual(item.image_file_id, file_row.id)

        patch_clear = self.api.patch(
            f'/api/v1/inventory/items/{item.id}/',
            {'imageFileId': None, 'image': ''},
            format='json',
        )
        self.assertEqual(patch_clear.status_code, 200)
        self.assertIsNone(patch_clear.data.get('imageUrl'))
        self.assertIsNone(patch_clear.data.get('image'))

        item.refresh_from_db()
        self.assertIsNone(item.image_file_id)

    def test_portal_orderable_items_returns_product_image(self):
        file_row = File.objects.create(
            client=self.tenant,
            scope='product_image',
            storage_key=file_service.build_storage_key(self.tenant.id, 'product_image', 'part.png'),
            file_name='part.png',
            content_type='image/png',
            file_size=64,
            status='committed',
        )
        file_service.write_bytes(file_row.storage_key, b'image-content')

        item_with_img = Item.objects.create(
            client=self.tenant,
            sku='SKU-PORTAL-01',
            name='Precision Bearing',
            category=self.category,
            selling_price=150,
            image_file=file_row,
            lifecycle_status='Active',
        )
        item_no_img = Item.objects.create(
            client=self.tenant,
            sku='SKU-PORTAL-02',
            name='Raw Steel Billet',
            category=self.category,
            selling_price=300,
            lifecycle_status='Active',
        )

        so = SalesOrder.objects.create(
            client=self.tenant,
            party=self.customer,
            stage='Confirmed',
            doc_date=date.today(),
            order_number='SO-PRIOR-01',
        )
        SalesOrderLine.objects.create(
            client=self.tenant,
            sales_order=so,
            item=item_with_img,
            qty=10,
            rate=140,
            line_no=1,
        )
        SalesOrderLine.objects.create(
            client=self.tenant,
            sales_order=so,
            item=item_no_img,
            qty=5,
            rate=290,
            line_no=2,
        )

        cust_tokens = build_tokens(self.customer_user)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {cust_tokens['access']}")

        res = self.api.get('/api/v1/portal/client/orderable-items/')
        self.assertEqual(res.status_code, 200)
        items = res.data.get('results', res.data)
        self.assertEqual(len(items), 2)

        bearing = next(i for i in items if i['id'] == str(item_with_img.id))
        self.assertIsNotNone(bearing.get('imageUrl'))
        self.assertIn(f'/files/{file_row.id}/download/', bearing['imageUrl'])

        billet = next(i for i in items if i['id'] == str(item_no_img.id))
        self.assertIsNone(billet.get('imageUrl'))
        self.assertIsNone(billet.get('image'))
