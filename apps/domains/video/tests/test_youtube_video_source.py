from __future__ import annotations

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.student_app.media.views import (
    StudentSessionVideoListView,
    StudentVideoMeView,
    StudentVideoPlaybackView,
    StudentVideoStatsView,
)
from apps.domains.video.models import Video
from apps.domains.video.views.video_views import VideoViewSet
from apps.domains.video.youtube import extract_youtube_video_id


User = get_user_model()


class YouTubeVideoSourceTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.Enrollment = apps.get_model("enrollment", "Enrollment")
        self.Lecture = apps.get_model("lectures", "Lecture")
        self.Session = apps.get_model("lectures", "Session")
        self.Student = apps.get_model("students", "Student")
        self.tenant = Tenant.objects.create(
            name="YouTube Video Tenant",
            code="youtube-video",
            is_active=True,
        )
        self.staff_user = User.objects.create_user(
            username="youtube_video_admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.staff_user, role="admin")

        self.lecture = self.Lecture.objects.create(
            tenant=self.tenant,
            title="YouTube Lecture",
            name="YouTube Lecture",
            subject="MATH",
        )
        self.session = self.Session.objects.create(
            lecture=self.lecture,
            title="1차시",
            order=1,
        )

    def _create_student(self, index: int = 1):
        student_user = User.objects.create_user(
            username=f"youtube_video_student_{index}",
            password="test1234",
            tenant=self.tenant,
        )
        student = self.Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            ps_number=f"YV-{index}",
            omr_code=f"{index:08d}"[-8:],
            name=f"YouTube Student {index}",
            parent_phone=f"0109876{index:04d}",
        )
        enrollment = self.Enrollment.objects.create(
            tenant=self.tenant,
            student=student,
            lecture=self.lecture,
            status="ACTIVE",
        )
        apps.get_model("enrollment", "SessionEnrollment").objects.create(
            tenant=self.tenant,
            enrollment=enrollment,
            session=self.session,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )
        return student_user, student, enrollment

    def _post_youtube(self, payload: dict):
        request = self.factory.post("/api/v1/media/videos/youtube/", payload, format="json")
        request.tenant = self.tenant
        force_authenticate(request, user=self.staff_user)
        return VideoViewSet.as_view({"post": "create_youtube"})(request)

    def test_extracts_provided_youtube_share_url(self):
        video_id = extract_youtube_video_id(
            "https://youtu.be/VnqgmOJaMGc?si=HVOBHgoVObf0zukX"
        )

        self.assertEqual(video_id, "VnqgmOJaMGc")

    def test_teacher_can_create_ready_youtube_video(self):
        response = self._post_youtube(
            {
                "session": self.session.id,
                "title": "유튜브 링크 영상",
                "url": "https://youtu.be/VnqgmOJaMGc?si=HVOBHgoVObf0zukX",
                "allow_skip": True,
                "max_speed": 2,
                "show_watermark": False,
            }
        )

        self.assertEqual(response.status_code, 201, response.data)
        video = Video.objects.get(pk=response.data["video"]["id"])
        self.assertEqual(video.status, Video.Status.READY)
        self.assertEqual(video.source_type, Video.SourceType.YOUTUBE)
        self.assertEqual(video.youtube_video_id, "VnqgmOJaMGc")
        self.assertEqual(video.file_key, "")
        self.assertEqual(response.data["video"]["source_type"], "youtube")
        self.assertIn("i.ytimg.com/vi/VnqgmOJaMGc", response.data["video"]["thumbnail_url"])

    def test_student_playback_payload_uses_youtube_embed_url(self):
        student_user, _student, enrollment = self._create_student()
        video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="유튜브 재생",
            status=Video.Status.READY,
            source_type=Video.SourceType.YOUTUBE,
            youtube_video_id="VnqgmOJaMGc",
            youtube_url="https://www.youtube.com/watch?v=VnqgmOJaMGc",
            allow_skip=True,
            max_speed=2,
            show_watermark=False,
        )

        request = self.factory.post(
            f"/api/v1/student/video/videos/{video.id}/playback/"
            f"?enrollment={enrollment.id}",
        )
        request.tenant = self.tenant
        force_authenticate(request, user=student_user)

        response = StudentVideoPlaybackView.as_view()(request, video_id=video.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["hls_url"])
        self.assertEqual(response.data["video"]["source_type"], "youtube")
        self.assertEqual(response.data["video"]["youtube_video_id"], "VnqgmOJaMGc")
        self.assertIn("youtube.com/embed/VnqgmOJaMGc", response.data["play_url"])
        self.assertEqual(response.data["policy"]["source"]["provider"], "youtube")

    def test_student_playlist_sorts_same_title_by_numeric_suffix(self):
        student_user, _student, enrollment = self._create_student(index=2)
        Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="영상이름 - 2",
            order=1,
            status=Video.Status.READY,
            file_key="videos/raw/2.mp4",
        )
        Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="영상이름 - 10",
            order=2,
            status=Video.Status.READY,
            file_key="videos/raw/10.mp4",
        )
        Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="영상이름 - 1",
            order=3,
            status=Video.Status.READY,
            file_key="videos/raw/1.mp4",
        )

        request = self.factory.get(
            f"/api/v1/student/video/sessions/{self.session.id}/videos/",
            {"enrollment": enrollment.id},
        )
        request.tenant = self.tenant
        force_authenticate(request, user=student_user)

        response = StudentSessionVideoListView.as_view()(request, session_id=self.session.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [item["title"] for item in response.data["items"]],
            ["영상이름 - 1", "영상이름 - 2", "영상이름 - 10"],
        )

    def test_ended_lecture_is_removed_from_student_video_access(self):
        student_user, _student, enrollment = self._create_student(index=3)
        video = Video.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="종료 강의 영상",
            status=Video.Status.READY,
            source_type=Video.SourceType.YOUTUBE,
            youtube_video_id="VnqgmOJaMGc",
            youtube_url="https://www.youtube.com/watch?v=VnqgmOJaMGc",
        )
        self.lecture.is_active = False
        self.lecture.save(update_fields=["is_active", "updated_at"])

        def get(path: str):
            request = self.factory.get(path, {"enrollment": enrollment.id})
            request.tenant = self.tenant
            force_authenticate(request, user=student_user)
            return request

        playlist_response = StudentSessionVideoListView.as_view()(
            get(f"/api/v1/student/video/sessions/{self.session.id}/videos/"),
            session_id=self.session.id,
        )
        playback_request = self.factory.post(
            f"/api/v1/student/video/videos/{video.id}/playback/"
            f"?enrollment={enrollment.id}"
        )
        playback_request.tenant = self.tenant
        force_authenticate(playback_request, user=student_user)
        playback_response = StudentVideoPlaybackView.as_view()(
            playback_request, video_id=video.id
        )
        home_response = StudentVideoMeView.as_view()(
            get("/api/v1/student/video/me/"),
        )
        stats_response = StudentVideoStatsView.as_view()(
            get("/api/v1/student/video/me/stats/"),
        )

        self.assertEqual(playlist_response.status_code, 403)
        self.assertEqual(playback_response.status_code, 403)
        self.assertEqual(home_response.status_code, 200)
        self.assertEqual(home_response.data["lectures"], [])
        self.assertEqual(stats_response.status_code, 200)
        self.assertEqual(stats_response.data["total_videos"], 0)
