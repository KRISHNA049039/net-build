from rest_framework.routers import DefaultRouter

from apps.catalog.submodules.orders import OrdersViewSet
from apps.catalog.submodules.products import ProductsViewSet
from apps.catalog.submodules.inventory import InventoryViewSet
from apps.catalog.submodules.shipments import ShipmentsViewSet

router = DefaultRouter()
router.register(r"orders", OrdersViewSet, basename="orders")
router.register(r"products", ProductsViewSet, basename="products")
router.register(r"inventory", InventoryViewSet, basename="inventory")
router.register(r"shipments", ShipmentsViewSet, basename="shipments")

urlpatterns = router.urls