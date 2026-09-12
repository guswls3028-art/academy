
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    VideoViewSet,
    VideoPermissionViewSet,
    DirectVideoEntitlementViewSet,
    InactiveVideoEntitlementViewSet,
    VideoProgressViewSet,
    VideoProgressView,  # ✅ 추가: Redis-only progress endpoint
    VideoPlaybackEventViewSet,
    VideoProcessingCompleteView,
    VideoPolicyImpactAPIView,
)

from .views.achievement_views import VideoAchievementView
from .views.playback_views import (
    PlaybackStartView,
    PlaybackRefreshView,
    PlaybackRenewView,
    PlaybackHeartbeatView,
    PlaybackEndView,
    PlaybackEventBatchView,
    PlaybackV2EventBatchView,
    PlaybackV2EndView,
)
from .views.admin_social_views import (
    AdminVideoCommentListView,
    AdminVideoCommentDetailView,
    AdminVideoEngagementView,
)
from .views.admin_landing_stats_view import AdminVideosLandingStatsView

# ========================================================
# Router
# ========================================================

router = DefaultRouter()
router.register(r"videos", VideoViewSet, basename="videos")
router.register(r"video-permissions", VideoPermissionViewSet, basename="video-permissions")
router.register(
    r"direct-video-entitlements",
    DirectVideoEntitlementViewSet,
    basename="direct-video-entitlements",
)
router.register(
    r"inactive-video-entitlements",
    InactiveVideoEntitlementViewSet,
    basename="inactive-video-entitlements",
)
router.register(r"video-progress", VideoProgressViewSet, basename="video-progress")
router.register(r"video-playback-events", VideoPlaybackEventViewSet, basename="video-playback-events")

# ========================================================
# urlpatterns (선언 먼저!)
# ========================================================

urlpatterns = [
    path("", include(router.urls)),
]

# ========================================================
# Nested / Extra APIs
# ========================================================

video_detail = VideoViewSet.as_view({"get": "retrieve"})
video_stats = VideoViewSet.as_view({"get": "stats"})

urlpatterns += [
    path(
        "lectures/<int:lecture_id>/sessions/<int:session_id>/videos/<int:pk>/",
        video_detail,
        name="video-video-detail-nested",
    ),
    path(
        "lectures/<int:lecture_id>/sessions/<int:session_id>/videos/<int:pk>/stats/",
        video_stats,
        name="video-video-stats-nested",
    ),
    path(
        "videos/<int:video_id>/achievement/",
        VideoAchievementView.as_view(),
        name="video-video-achievement",
    ),
]

# ========================================================
# Playback APIs (Student)
# ========================================================

urlpatterns += [
    path("playback/start/", PlaybackStartView.as_view()),
    path("playback/refresh/", PlaybackRefreshView.as_view()),
    path("playback/renew/", PlaybackRenewView.as_view()),
    path("playback/heartbeat/", PlaybackHeartbeatView.as_view()),
    path("playback/end/", PlaybackEndView.as_view()),
    path("playback/events/", PlaybackEventBatchView.as_view()),
    path("playback/v2/events/", PlaybackV2EventBatchView.as_view()),
    path("playback/v2/end/", PlaybackV2EndView.as_view()),
]

# ========================================================
# Internal (Legacy ACK - kept)
# ========================================================

urlpatterns += [
    path(
        "internal/videos/<int:video_id>/processing-complete/",
        VideoProcessingCompleteView.as_view(),
        name="video-video-processing-complete",
    ),
]

# ========================================================
# Policy Impact (Admin preview)
# ========================================================

urlpatterns += [
    path(
        "videos/<int:video_id>/policy-impact/",
        VideoPolicyImpactAPIView.as_view(),
        name="video-video-policy-impact",
    ),
    # ✅ Progress endpoint 추가 (Redis-only)
    path(
        "videos/<int:pk>/progress/",
        VideoProgressView.as_view(),
        name="video-progress",
    ),
]

# ========================================================
# Admin Video Social (Comments, Engagement)
# ========================================================

urlpatterns += [
    path(
        "videos/<int:video_id>/comments/",
        AdminVideoCommentListView.as_view(),
        name="admin-video-comments",
    ),
    path(
        "videos/comments/<int:comment_id>/",
        AdminVideoCommentDetailView.as_view(),
        name="admin-video-comment-detail",
    ),
    path(
        "videos/<int:video_id>/engagement/",
        AdminVideoEngagementView.as_view(),
        name="admin-video-engagement",
    ),
]

# ========================================================
# Admin Videos Landing Stats (KPI inbox)
# ========================================================

urlpatterns += [
    path(
        "admin/videos/landing-stats/",
        AdminVideosLandingStatsView.as_view(),
        name="admin-videos-landing-stats",
    ),
]
