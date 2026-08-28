from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.ff_net.submodules.ff_net_repository import FFNetViewSet
from apps.ff_net.views import (
    NetBuildView,
    NetMergeApproveView,
    NetMergeCandidatesView,
    NetMergeRejectView,
)

router = DefaultRouter()
router.register(r"reports", FFNetViewSet, basename="ff-net-reports")

urlpatterns = [
    path("build/", NetBuildView.as_view(), name="ff-net-build"),
    path("merge/candidates/", NetMergeCandidatesView.as_view(),
         name="ff-net-merge-candidates"),
    path("merge/candidates/<str:pk>/approve/", NetMergeApproveView.as_view(),
         name="ff-net-merge-approve"),
    path("merge/candidates/<str:pk>/reject/", NetMergeRejectView.as_view(),
         name="ff-net-merge-reject"),
] + router.urls
