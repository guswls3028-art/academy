from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models.tenant import Tenant
from apps.core.models.tenant_membership import TenantMembership
from apps.domains.community.api.views.admin_views import AdminPostViewSet
from apps.domains.community.api.views.post_views import PostViewSet
from apps.domains.community.api.views.scope_node_views import ScopeNodeViewSet
from apps.domains.community.models import (
    CommunityReport,
    CommunityUserBlock,
    PostAttachment,
    PostEntity,
    PostLike,
    PostMapping,
    PostReply,
    ScopeNode,
)
User = get_user_model()
Enrollment = apps.get_model("enrollment", "Enrollment")
Lecture = apps.get_model("lectures", "Lecture")
Parent = apps.get_model("parents", "Parent")
Student = apps.get_model("students", "Student")


class CommunityHardeningFixture(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="Community", code="comm_p0", is_active=True)
        self.other_tenant = Tenant.objects.create(name="Other", code="comm_p0_other", is_active=True)

        self.owner = User.objects.create_user(
            username="comm_owner", password="pw1234", tenant=self.tenant, name="Owner"
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.owner, role="owner")

        self.teacher = User.objects.create_user(
            username="comm_teacher", password="pw1234", tenant=self.tenant, name="Teacher"
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.teacher, role="teacher")

        self.student_user = User.objects.create_user(
            username="comm_student", password="pw1234", tenant=self.tenant, name="Student"
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.student_user, role="student")
        self.student = Student.objects.create(
            tenant=self.tenant,
            user=self.student_user,
            ps_number="S001",
            name="Student",
            phone="01011112222",
            parent_phone="01033334444",
            omr_code="11112222",
        )

        self.parent_user = User.objects.create_user(
            username="comm_parent", password="pw1234", tenant=self.tenant, name="Parent"
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.parent_user, role="parent")
        self.parent = Parent.objects.create(
            tenant=self.tenant,
            user=self.parent_user,
            name="Parent",
            phone="01033334444",
        )
        self.student.parent = self.parent
        self.student.save(update_fields=["parent"])

        self.visible_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Visible Lecture",
            name="Visible Lecture",
            subject="math",
        )
        self.hidden_lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Hidden Lecture",
            name="Hidden Lecture",
            subject="math",
        )
        self.foreign_lecture = Lecture.objects.create(
            tenant=self.other_tenant,
            title="Foreign Lecture",
            name="Foreign Lecture",
            subject="math",
        )
        Enrollment.objects.create(
            tenant=self.tenant,
            student=self.student,
            lecture=self.visible_lecture,
            status="ACTIVE",
        )

        self.visible_node = ScopeNode.objects.create(
            tenant=self.tenant,
            level=ScopeNode.Level.COURSE,
            lecture=self.visible_lecture,
        )
        self.hidden_node = ScopeNode.objects.create(
            tenant=self.tenant,
            level=ScopeNode.Level.COURSE,
            lecture=self.hidden_lecture,
        )
        self.foreign_node = ScopeNode.objects.create(
            tenant=self.other_tenant,
            level=ScopeNode.Level.COURSE,
            lecture=self.foreign_lecture,
        )

        self.visible_post = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="board",
            title="Visible board",
            content="visible",
            author_role="staff",
            status="published",
        )
        PostMapping.objects.create(post=self.visible_post, node=self.visible_node)

        self.hidden_post = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="board",
            title="Hidden board",
            content="hidden",
            author_role="staff",
            status="published",
        )
        PostMapping.objects.create(post=self.hidden_post, node=self.hidden_node)
        self.hidden_reply = PostReply.objects.create(
            tenant=self.tenant,
            post=self.hidden_post,
            content="hidden reply",
            author_role="staff",
        )
        self.hidden_attachment = PostAttachment.objects.create(
            tenant=self.tenant,
            post=self.hidden_post,
            r2_key="tenants/1/community/posts/hidden/file.pdf",
            original_name="file.pdf",
            size_bytes=5,
            content_type="application/pdf",
        )

    def _request(self, method, user, path, data=None, format="json", **extra):
        request = getattr(self.factory, method)(path, data=data or {}, format=format, **extra)
        force_authenticate(request, user=user)
        request.tenant = self.tenant
        return request


class TestScopedPostMappingVisibility(CommunityHardeningFixture):
    @patch("apps.domains.community.api.views.post_views.get_reply_event_notifier")
    def test_reply_notification_uses_reply_id_as_occurrence_identity(self, get_notifier):
        notifier = get_notifier.return_value
        notifier.return_value = True
        post = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="중복 방지 질문",
            content="질문",
            created_by=self.student,
            author_role="student",
            status="published",
        )
        PostMapping.objects.create(post=post, node=self.visible_node)

        response = PostViewSet.as_view({"post": "replies"})(
            self._request(
                "post",
                self.teacher,
                f"/api/v1/community/posts/{post.id}/replies/",
                {"content": "답변"},
            ),
            pk=post.id,
        )

        self.assertEqual(response.status_code, 201, response.data)
        notifier.assert_called_once()
        context = notifier.call_args.kwargs["context"]
        self.assertEqual(context["_domain_object_id"], f"reply:{response.data['id']}")
        self.assertEqual(context["_source_domain"], "community")

    def test_student_direct_actions_require_visible_post_mapping(self):
        retrieve = PostViewSet.as_view({"get": "retrieve"})
        response = retrieve(
            self._request("get", self.student_user, f"/api/v1/community/posts/{self.hidden_post.id}/"),
            pk=self.hidden_post.id,
        )
        self.assertEqual(response.status_code, 404)

        replies = PostViewSet.as_view({"get": "replies"})
        response = replies(
            self._request("get", self.student_user, f"/api/v1/community/posts/{self.hidden_post.id}/replies/"),
            pk=self.hidden_post.id,
        )
        self.assertEqual(response.status_code, 404)

        download = PostViewSet.as_view({"get": "download_attachment"})
        response = download(
            self._request(
                "get",
                self.student_user,
                f"/api/v1/community/posts/{self.hidden_post.id}/attachments/{self.hidden_attachment.id}/download/",
            ),
            pk=self.hidden_post.id,
            att_id=self.hidden_attachment.id,
        )
        self.assertEqual(response.status_code, 404)

        like = PostViewSet.as_view({"post": "like"})
        response = like(
            self._request("post", self.student_user, f"/api/v1/community/posts/{self.hidden_post.id}/like/"),
            pk=self.hidden_post.id,
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PostLike.objects.filter(post=self.hidden_post, user=self.student_user).exists())

        report = PostViewSet.as_view({"post": "report_post"})
        response = report(
            self._request(
                "post",
                self.student_user,
                f"/api/v1/community/posts/{self.hidden_post.id}/report/",
                {"reason": CommunityReport.REASON_OTHER},
            ),
            pk=self.hidden_post.id,
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CommunityReport.objects.filter(target_id=self.hidden_post.id).exists())

    def test_student_node_list_rejects_foreign_node_scope(self):
        view = PostViewSet.as_view({"get": "list"})
        response = view(
            self._request("get", self.student_user, f"/api/v1/community/posts/?node_id={self.hidden_node.id}")
        )
        self.assertEqual(response.status_code, 200)
        results = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual(len(results), 0)

    @patch("apps.domains.community.services.qna_notifications.notify_qna_created")
    def test_student_without_enrollment_can_create_and_read_own_qna(self, mock_notify):
        Enrollment.objects.filter(tenant=self.tenant, student=self.student).delete()
        create = PostViewSet.as_view({"post": "create"})
        response = create(
            self._request(
                "post",
                self.student_user,
                "/api/v1/community/posts/",
                {
                    "post_type": "qna",
                    "title": "수강 전 질문",
                    "content": "아직 듣는 강의가 없어도 질문합니다.",
                    "node_ids": [],
                },
            )
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["post_type"], "qna")
        self.assertEqual(response.data["created_by"], self.student.id)
        mock_notify.assert_called_once()

        list_view = PostViewSet.as_view({"get": "list"})
        response = list_view(
            self._request("get", self.student_user, "/api/v1/community/posts/?post_type=qna")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data], [PostEntity.objects.latest("id").id])

    def test_staff_post_list_applies_q_filter_on_generic_and_typed_routes(self):
        self.visible_post.category_label = "Algebra scope"
        self.visible_post.save(update_fields=["category_label"])

        generic = PostViewSet.as_view({"get": "list"})
        response = generic(
            self._request("get", self.teacher, "/api/v1/community/posts/?post_type=board&q=Algebra")
        )
        self.assertEqual(response.status_code, 200)
        results = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual([post["id"] for post in results], [self.visible_post.id])

        response = generic(
            self._request("get", self.teacher, "/api/v1/community/posts/?post_type=board&q=NoSuchE2EPost")
        )
        self.assertEqual(response.status_code, 200)
        results = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual(results, [])

        typed = PostViewSet.as_view({"get": "board"})
        response = typed(self._request("get", self.teacher, "/api/v1/community/posts/board/?q=Algebra"))
        self.assertEqual(response.status_code, 200)
        results = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual([post["id"] for post in results], [self.visible_post.id])

    @patch("apps.infrastructure.storage.r2.generate_presigned_get_url_storage", return_value="https://example.test/file.pdf")
    def test_student_can_still_access_visible_mapped_post(self, _mock_url):
        visible_attachment = PostAttachment.objects.create(
            tenant=self.tenant,
            post=self.visible_post,
            r2_key="tenants/1/community/posts/visible/file.pdf",
            original_name="visible.pdf",
            size_bytes=5,
            content_type="application/pdf",
        )
        retrieve = PostViewSet.as_view({"get": "retrieve"})
        response = retrieve(
            self._request("get", self.student_user, f"/api/v1/community/posts/{self.visible_post.id}/"),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 200)

        download = PostViewSet.as_view({"get": "download_attachment"})
        response = download(
            self._request(
                "get",
                self.student_user,
                f"/api/v1/community/posts/{self.visible_post.id}/attachments/{visible_attachment.id}/download/",
            ),
            pk=self.visible_post.id,
            att_id=visible_attachment.id,
        )
        self.assertEqual(response.status_code, 200)


class TestParentCommunityReadOnly(CommunityHardeningFixture):
    @patch("apps.infrastructure.storage.r2.generate_presigned_get_url_storage")
    @patch("apps.infrastructure.storage.r2.upload_fileobj_to_r2_storage")
    def test_parent_selected_child_can_create_questions_counsel_and_attach(
        self,
        upload_file,
        presign,
    ):
        presign.return_value = "https://storage.example/parent-question.pdf"
        create = PostViewSet.as_view({"post": "create"})
        created_posts = []
        for post_type in ("qna", "counsel"):
            response = create(
                self._request(
                    "post",
                    self.parent_user,
                    "/api/v1/community/posts/",
                    {
                        "title": f"Parent {post_type}",
                        "content": "selected child request",
                        "post_type": post_type,
                        "node_ids": [],
                    },
                    HTTP_X_STUDENT_ID=str(self.student.id),
                )
            )
            self.assertEqual(response.status_code, 201, response.data)
            post = PostEntity.objects.get(pk=response.data["id"])
            self.assertEqual(post.created_by_id, self.student.id)
            self.assertEqual(post.author_role, "parent")
            self.assertEqual(post.status, "published")
            created_posts.append(post)

        qna = created_posts[0]
        upload = PostViewSet.as_view({"post": "upload_attachments"})
        upload_response = upload(
            self._request(
                "post",
                self.parent_user,
                f"/api/v1/community/posts/{qna.id}/attachments/",
                {
                    "files": [
                        SimpleUploadedFile(
                            "question.pdf",
                            b"%PDF-parent-question",
                            content_type="application/pdf",
                        )
                    ]
                },
                format="multipart",
                HTTP_X_STUDENT_ID=str(self.student.id),
            ),
            pk=qna.id,
        )
        self.assertEqual(upload_response.status_code, 201, upload_response.data)
        attachment = PostAttachment.objects.get(post=qna)
        self.assertEqual(attachment.original_name, "question.pdf")
        self.assertIn(
            f"tenants/{self.tenant.id}/community/posts/{qna.id}/",
            attachment.r2_key,
        )
        upload_file.assert_called_once()

        retrieve = PostViewSet.as_view({"get": "retrieve"})
        reload_response = retrieve(
            self._request(
                "get",
                self.parent_user,
                f"/api/v1/community/posts/{qna.id}/",
                HTTP_X_STUDENT_ID=str(self.student.id),
            ),
            pk=qna.id,
        )
        self.assertEqual(reload_response.status_code, 200, reload_response.data)
        self.assertEqual(
            [item["id"] for item in reload_response.data["attachments"]],
            [attachment.id],
        )

        delete_attachment = PostViewSet.as_view({"delete": "delete_attachment"})
        with patch(
            "apps.infrastructure.storage.r2.delete_object_r2_storage"
        ) as delete_file:
            delete_attachment_response = delete_attachment(
                self._request(
                    "delete",
                    self.parent_user,
                    (
                        f"/api/v1/community/posts/{qna.id}/attachments/"
                        f"{attachment.id}/"
                    ),
                    HTTP_X_STUDENT_ID=str(self.student.id),
                ),
                pk=qna.id,
                att_id=attachment.id,
            )
        self.assertEqual(delete_attachment_response.status_code, 204)
        delete_file.assert_called_once_with(key=attachment.r2_key)

        destroy = PostViewSet.as_view({"delete": "destroy"})
        for post in created_posts:
            delete_response = destroy(
                self._request(
                    "delete",
                    self.parent_user,
                    f"/api/v1/community/posts/{post.id}/",
                    HTTP_X_STUDENT_ID=str(self.student.id),
                ),
                pk=post.id,
            )
            self.assertEqual(delete_response.status_code, 204, delete_response.data)
        self.assertFalse(
            PostEntity.objects.filter(id__in=[post.id for post in created_posts]).exists()
        )

    def test_parent_community_writes_require_exact_owned_selected_child(self):
        other_student_user = User.objects.create_user(
            username="comm_unowned_write",
            password="pw1234",
            tenant=self.tenant,
            name="Unowned",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=other_student_user,
            role="student",
        )
        unowned = Student.objects.create(
            tenant=self.tenant,
            user=other_student_user,
            ps_number="S-WRITE-OTHER",
            name="Unowned",
            omr_code="77770000",
        )
        foreign_user = User.objects.create_user(
            username="comm_foreign_write",
            password="pw1234",
            tenant=self.other_tenant,
            name="Foreign",
        )
        foreign = Student.objects.create(
            tenant=self.other_tenant,
            user=foreign_user,
            ps_number="S-WRITE-FOREIGN",
            name="Foreign",
            omr_code="77770002",
        )
        create = PostViewSet.as_view({"post": "create"})
        for selected_id in (None, unowned.id, foreign.id):
            extra = {}
            if selected_id is not None:
                extra["HTTP_X_STUDENT_ID"] = str(selected_id)
            response = create(
                self._request(
                    "post",
                    self.parent_user,
                    "/api/v1/community/posts/",
                    {
                        "title": f"Denied {selected_id}",
                        "content": "must not persist",
                        "post_type": "qna",
                    },
                    **extra,
                )
            )
            self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(PostEntity.objects.filter(title__startswith="Denied ").exists())

        forged_author = create(
            self._request(
                "post",
                self.parent_user,
                "/api/v1/community/posts/",
                {
                    "title": "Server-owned author",
                    "content": "forged author must be ignored",
                    "post_type": "qna",
                    "created_by": unowned.id,
                },
                HTTP_X_STUDENT_ID=str(self.student.id),
            )
        )
        self.assertEqual(forged_author.status_code, 201, forged_author.data)
        forged_post = PostEntity.objects.get(pk=forged_author.data["id"])
        self.assertEqual(forged_post.created_by_id, self.student.id)
        self.assertEqual(forged_post.author_role, "parent")

        sibling_user = User.objects.create_user(
            username="comm_sibling_write",
            password="pw1234",
            tenant=self.tenant,
            name="Sibling",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=sibling_user,
            role="student",
        )
        sibling = Student.objects.create(
            tenant=self.tenant,
            user=sibling_user,
            parent=self.parent,
            ps_number="S-WRITE-SIBLING",
            name="Sibling",
            omr_code="77770001",
        )
        sibling_qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Sibling qna",
            content="private",
            created_by=sibling,
            author_role="parent",
            status="published",
        )
        upload = PostViewSet.as_view({"post": "upload_attachments"})
        response = upload(
            self._request(
                "post",
                self.parent_user,
                f"/api/v1/community/posts/{sibling_qna.id}/attachments/",
                {
                    "files": [
                        SimpleUploadedFile(
                            "wrong-child.pdf",
                            b"%PDF-denied",
                            content_type="application/pdf",
                        )
                    ]
                },
                format="multipart",
                HTTP_X_STUDENT_ID=str(self.student.id),
            ),
            pk=sibling_qna.id,
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(PostAttachment.objects.filter(post=sibling_qna).exists())

    def test_limited_reader_scope_nodes_follow_active_enrollment(self):
        view = ScopeNodeViewSet.as_view({"get": "list"})

        parent_response = view(
            self._request("get", self.parent_user, "/api/v1/community/scope-nodes/")
        )
        student_response = view(
            self._request("get", self.student_user, "/api/v1/community/scope-nodes/")
        )
        owner_response = view(
            self._request("get", self.owner, "/api/v1/community/scope-nodes/")
        )

        self.assertEqual(parent_response.status_code, 200)
        parent_ids = {row["id"] for row in parent_response.data}
        self.assertIn(self.visible_node.id, parent_ids)
        self.assertNotIn(self.hidden_node.id, parent_ids)

        self.assertEqual(student_response.status_code, 200)
        student_ids = {row["id"] for row in student_response.data}
        self.assertIn(self.visible_node.id, student_ids)
        self.assertNotIn(self.hidden_node.id, student_ids)

        self.assertEqual(owner_response.status_code, 200)
        owner_ids = {row["id"] for row in owner_response.data}
        self.assertIn(self.visible_node.id, owner_ids)
        self.assertIn(self.hidden_node.id, owner_ids)

    def test_parent_board_list_and_counts_follow_child_enrollments(self):
        board = PostViewSet.as_view({"get": "board"})
        response = board(
            self._request("get", self.parent_user, "/api/v1/community/posts/board/?page_size=20")
        )

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.data["results"]}
        self.assertIn(self.visible_post.id, ids)
        self.assertNotIn(self.hidden_post.id, ids)

        counts = PostViewSet.as_view({"get": "counts"})
        response = counts(
            self._request("get", self.parent_user, "/api/v1/community/posts/counts/?post_type=board")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(response.data["by_node_id"], {self.visible_node.id: 1})
        self.assertNotIn(self.hidden_node.id, response.data["by_node_id"])

    def test_parent_board_list_and_counts_follow_selected_child_only(self):
        second_student_user = User.objects.create_user(
            username="comm_second_child",
            password="pw1234",
            tenant=self.tenant,
            name="Second Child",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=second_student_user,
            role="student",
        )
        second_student = Student.objects.create(
            tenant=self.tenant,
            user=second_student_user,
            ps_number="S002",
            name="Second Child",
            phone="01055550000",
            parent_phone="01033334444",
            omr_code="55550000",
            parent=self.parent,
        )
        Enrollment.objects.create(
            tenant=self.tenant,
            student=second_student,
            lecture=self.hidden_lecture,
            status="ACTIVE",
        )

        board = PostViewSet.as_view({"get": "board"})
        response = board(
            self._request(
                "get",
                self.parent_user,
                "/api/v1/community/posts/board/?page_size=20",
                HTTP_X_STUDENT_ID=str(self.student.id),
            )
        )

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.data["results"]}
        self.assertIn(self.visible_post.id, ids)
        self.assertNotIn(self.hidden_post.id, ids)

        counts = PostViewSet.as_view({"get": "counts"})
        response = counts(
            self._request(
                "get",
                self.parent_user,
                "/api/v1/community/posts/counts/?post_type=board",
                HTTP_X_STUDENT_ID=str(self.student.id),
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(response.data["by_node_id"], {self.visible_node.id: 1})

        second_child_response = board(
            self._request(
                "get",
                self.parent_user,
                "/api/v1/community/posts/board/?page_size=20",
                HTTP_X_STUDENT_ID=str(second_student.id),
            )
        )
        self.assertEqual(second_child_response.status_code, 200)
        second_child_ids = {row["id"] for row in second_child_response.data["results"]}
        self.assertNotIn(self.visible_post.id, second_child_ids)
        self.assertIn(self.hidden_post.id, second_child_ids)

    def test_limited_reader_counts_hide_private_post_types(self):
        other_student_user = User.objects.create_user(
            username="comm_other_student",
            password="pw1234",
            tenant=self.tenant,
            name="Other Student",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=other_student_user,
            role="student",
        )
        other_student = Student.objects.create(
            tenant=self.tenant,
            user=other_student_user,
            ps_number="S002",
            name="Other Student",
            phone="01055556666",
            parent_phone="01077778888",
            omr_code="22223333",
        )
        Enrollment.objects.create(
            tenant=self.tenant,
            student=other_student,
            lecture=self.visible_lecture,
            status="ACTIVE",
        )
        qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Other qna",
            content="private",
            created_by=other_student,
            author_role="student",
            status="published",
        )
        PostMapping.objects.create(post=qna, node=self.visible_node)
        PostEntity.objects.create(
            tenant=self.tenant,
            post_type="counsel",
            title="Global counsel",
            content="private",
            created_by=other_student,
            author_role="student",
            status="published",
        )

        counts = PostViewSet.as_view({"get": "counts"})
        for user in (self.student_user, self.parent_user):
            for post_type in ("qna", "counsel"):
                response = counts(
                    self._request(
                        "get",
                        user,
                        f"/api/v1/community/posts/counts/?post_type={post_type}",
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["total"], 0)
                self.assertEqual(response.data["global_count"], 0)
                self.assertEqual(response.data["by_node_id"], {})

        owner_qna_response = counts(
            self._request("get", self.owner, "/api/v1/community/posts/counts/?post_type=qna")
        )
        self.assertEqual(owner_qna_response.status_code, 200)
        self.assertEqual(owner_qna_response.data["total"], 1)

    def test_parent_can_read_child_private_qna_and_counsel_only(self):
        qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Child qna",
            content="private",
            created_by=self.student,
            author_role="student",
            status="published",
        )
        counsel = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="counsel",
            title="Child counsel",
            content="private",
            created_by=self.student,
            author_role="student",
            status="published",
        )
        reply = PostReply.objects.create(
            tenant=self.tenant,
            post=qna,
            content="answer",
            author_role="staff",
        )
        other_student_user = User.objects.create_user(
            username="comm_other_private",
            password="pw1234",
            tenant=self.tenant,
            name="Other Student",
        )
        other_student = Student.objects.create(
            tenant=self.tenant,
            user=other_student_user,
            ps_number="S009",
            name="Other Student",
            omr_code="99990000",
        )
        other_qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Other qna",
            content="private",
            created_by=other_student,
            author_role="student",
            status="published",
        )

        list_view = PostViewSet.as_view({"get": "list"})
        response = list_view(
            self._request("get", self.parent_user, "/api/v1/community/posts/?post_type=qna&page_size=100")
        )
        self.assertEqual(response.status_code, 200)
        rows = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        ids = {row["id"] for row in rows}
        self.assertIn(qna.id, ids)
        self.assertNotIn(other_qna.id, ids)

        retrieve = PostViewSet.as_view({"get": "retrieve"})
        self.assertEqual(
            retrieve(
                self._request("get", self.parent_user, f"/api/v1/community/posts/{qna.id}/"),
                pk=qna.id,
            ).status_code,
            200,
        )
        self.assertEqual(
            retrieve(
                self._request("get", self.parent_user, f"/api/v1/community/posts/{counsel.id}/"),
                pk=counsel.id,
            ).status_code,
            200,
        )
        self.assertEqual(
            retrieve(
                self._request("get", self.parent_user, f"/api/v1/community/posts/{other_qna.id}/"),
                pk=other_qna.id,
            ).status_code,
            404,
        )

        replies = PostViewSet.as_view({"get": "replies"})
        response = replies(
            self._request("get", self.parent_user, f"/api/v1/community/posts/{qna.id}/replies/"),
            pk=qna.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [reply.id])

    def test_parent_cannot_write_replies_or_attachments(self):
        reply = PostReply.objects.create(
            tenant=self.tenant,
            post=self.visible_post,
            content="student reply",
            created_by=self.student,
            author_role="student",
        )
        attachment = PostAttachment.objects.create(
            tenant=self.tenant,
            post=self.visible_post,
            r2_key="tenants/1/community/posts/visible/delete.pdf",
            original_name="delete.pdf",
            size_bytes=5,
            content_type="application/pdf",
        )

        cases = [
            (
                PostViewSet.as_view({"post": "replies"}),
                self._request(
                    "post",
                    self.parent_user,
                    f"/api/v1/community/posts/{self.visible_post.id}/replies/",
                    {"content": "parent reply"},
                ),
                {"pk": self.visible_post.id},
            ),
            (
                PostViewSet.as_view({"patch": "reply_detail"}),
                self._request(
                    "patch",
                    self.parent_user,
                    f"/api/v1/community/posts/{self.visible_post.id}/replies/{reply.id}/",
                    {"content": "edited"},
                ),
                {"pk": self.visible_post.id, "reply_id": reply.id},
            ),
            (
                PostViewSet.as_view({"delete": "reply_detail"}),
                self._request(
                    "delete",
                    self.parent_user,
                    f"/api/v1/community/posts/{self.visible_post.id}/replies/{reply.id}/",
                ),
                {"pk": self.visible_post.id, "reply_id": reply.id},
            ),
            (
                PostViewSet.as_view({"post": "upload_attachments"}),
                self._request(
                    "post",
                    self.parent_user,
                    f"/api/v1/community/posts/{self.visible_post.id}/attachments/",
                    {"files": [SimpleUploadedFile("parent.pdf", b"%PDF-", content_type="application/pdf")]},
                    format="multipart",
                ),
                {"pk": self.visible_post.id},
            ),
            (
                PostViewSet.as_view({"delete": "delete_attachment"}),
                self._request(
                    "delete",
                    self.parent_user,
                    f"/api/v1/community/posts/{self.visible_post.id}/attachments/{attachment.id}/",
                ),
                {"pk": self.visible_post.id, "att_id": attachment.id},
            ),
        ]
        for view, request, kwargs in cases:
            response = view(request, **kwargs)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["code"], "parent_read_only")

        self.assertTrue(PostReply.objects.filter(id=reply.id, content="student reply").exists())
        self.assertTrue(PostAttachment.objects.filter(id=attachment.id).exists())
        self.assertEqual(PostAttachment.objects.filter(post=self.visible_post).count(), 1)

    def test_parent_cannot_like_or_report(self):
        like = PostViewSet.as_view({"post": "like"})
        response = like(
            self._request("post", self.parent_user, f"/api/v1/community/posts/{self.visible_post.id}/like/"),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PostLike.objects.filter(post=self.visible_post, user=self.parent_user).exists())

        report = PostViewSet.as_view({"post": "report_post"})
        response = report(
            self._request(
                "post",
                self.parent_user,
                f"/api/v1/community/posts/{self.visible_post.id}/report/",
                {"reason": CommunityReport.REASON_OTHER},
            ),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CommunityReport.objects.filter(reporter=self.parent_user).exists())


class TestCommunityNodeRemapping(CommunityHardeningFixture):
    def test_create_rejects_invalid_or_foreign_node_without_tenant_wide_fallback(self):
        view = PostViewSet.as_view({"post": "create"})
        before = PostEntity.objects.filter(tenant=self.tenant).count()
        response = view(
            self._request(
                "post",
                self.owner,
                "/api/v1/community/posts/",
                {
                    "title": "Scoped notice",
                    "content": "body",
                    "post_type": "board",
                    "node_ids": [self.visible_node.id, self.foreign_node.id],
                },
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PostEntity.objects.filter(tenant=self.tenant).count(), before)

    def test_teacher_cannot_remap_nodes(self):
        view = PostViewSet.as_view({"patch": "update_nodes"})
        response = view(
            self._request(
                "patch",
                self.teacher,
                f"/api/v1/community/posts/{self.visible_post.id}/nodes/",
                {"node_ids": [self.hidden_node.id]},
            ),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            list(PostMapping.objects.filter(post=self.visible_post).values_list("node_id", flat=True)),
            [self.visible_node.id],
        )

    def test_invalid_or_foreign_node_preserves_existing_mapping(self):
        view = PostViewSet.as_view({"patch": "update_nodes"})
        response = view(
            self._request(
                "patch",
                self.owner,
                f"/api/v1/community/posts/{self.visible_post.id}/nodes/",
                {"node_ids": [self.foreign_node.id]},
            ),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            list(PostMapping.objects.filter(post=self.visible_post).values_list("node_id", flat=True)),
            [self.visible_node.id],
        )

    def test_owner_can_remap_to_valid_tenant_node(self):
        view = PostViewSet.as_view({"patch": "update_nodes"})
        response = view(
            self._request(
                "patch",
                self.owner,
                f"/api/v1/community/posts/{self.visible_post.id}/nodes/",
                {"node_ids": [self.hidden_node.id]},
            ),
            pk=self.visible_post.id,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(PostMapping.objects.filter(post=self.visible_post).values_list("node_id", flat=True)),
            [self.hidden_node.id],
        )


class TestCommunityStudentWriteContract(CommunityHardeningFixture):
    def test_student_can_create_public_board_post(self):
        view = PostViewSet.as_view({"post": "create"})
        response = view(
            self._request(
                "post",
                self.student_user,
                "/api/v1/community/posts/",
                {
                    "title": "Student board",
                    "content": "<p>hello</p>",
                    "post_type": "board",
                    "node_ids": [],
                },
            )
        )

        self.assertEqual(response.status_code, 201)
        post = PostEntity.objects.get(title="Student board")
        self.assertEqual(post.post_type, "board")
        self.assertEqual(post.created_by, self.student)
        self.assertEqual(post.author_role, "student")
        self.assertEqual(post.status, "published")
        self.assertFalse(post.is_urgent)
        self.assertFalse(post.is_pinned)
        self.assertFalse(PostMapping.objects.filter(post=post).exists())

    def test_student_cannot_create_staff_managed_post_type(self):
        view = PostViewSet.as_view({"post": "create"})
        response = view(
            self._request(
                "post",
                self.student_user,
                "/api/v1/community/posts/",
                {
                    "title": "Student notice",
                    "content": "body",
                    "post_type": "notice",
                    "node_ids": [],
                },
            )
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PostEntity.objects.filter(title="Student notice").exists())

    def test_student_cannot_scope_private_question_to_arbitrary_node(self):
        view = PostViewSet.as_view({"post": "create"})
        response = view(
            self._request(
                "post",
                self.student_user,
                "/api/v1/community/posts/",
                {
                    "title": "Scoped qna",
                    "content": "body",
                    "post_type": "qna",
                    "node_ids": [self.hidden_node.id],
                },
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PostEntity.objects.filter(title="Scoped qna").exists())

    def test_student_cannot_create_with_operator_fields(self):
        view = PostViewSet.as_view({"post": "create"})
        response = view(
            self._request(
                "post",
                self.student_user,
                "/api/v1/community/posts/",
                {
                    "title": "Student pinned board",
                    "content": "body",
                    "post_type": "board",
                    "status": "published",
                    "is_pinned": True,
                },
            )
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PostEntity.objects.filter(title="Student pinned board").exists())

    def test_student_cannot_patch_operator_fields_on_own_post(self):
        own_qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Own qna",
            content="body",
            created_by=self.student,
            author_role="student",
            status="published",
        )

        view = PostViewSet.as_view({"patch": "partial_update"})
        response = view(
            self._request(
                "patch",
                self.student_user,
                f"/api/v1/community/posts/{own_qna.id}/",
                {
                    "title": "Tampered",
                    "author_role": "staff",
                    "post_type": "notice",
                    "status": "archived",
                    "is_pinned": True,
                },
            ),
            pk=own_qna.id,
        )

        self.assertEqual(response.status_code, 403)
        own_qna.refresh_from_db()
        self.assertEqual(own_qna.title, "Own qna")
        self.assertEqual(own_qna.author_role, "student")
        self.assertEqual(own_qna.post_type, "qna")
        self.assertEqual(own_qna.status, "published")
        self.assertFalse(own_qna.is_pinned)

    def test_student_can_patch_own_title_and_content(self):
        own_qna = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="qna",
            title="Own qna",
            content="body",
            created_by=self.student,
            author_role="student",
            status="published",
        )

        view = PostViewSet.as_view({"patch": "partial_update"})
        response = view(
            self._request(
                "patch",
                self.student_user,
                f"/api/v1/community/posts/{own_qna.id}/",
                {"title": "Updated qna", "content": "<script>alert(1)</script><p>safe</p>"},
            ),
            pk=own_qna.id,
        )

        self.assertEqual(response.status_code, 200)
        own_qna.refresh_from_db()
        self.assertEqual(own_qna.title, "Updated qna")
        self.assertNotIn("<script", own_qna.content)
        self.assertIn("<p>safe</p>", own_qna.content)


class TestCommunityReplyParentGuard(CommunityHardeningFixture):
    def test_reply_patch_cannot_reparent_to_reply_on_another_post(self):
        other_post = PostEntity.objects.create(
            tenant=self.tenant,
            post_type="board",
            title="Other board",
            content="body",
            author_role="staff",
            status="published",
        )
        own_reply = PostReply.objects.create(
            tenant=self.tenant,
            post=self.visible_post,
            content="own reply",
            author_role="staff",
        )
        other_reply = PostReply.objects.create(
            tenant=self.tenant,
            post=other_post,
            content="other reply",
            author_role="staff",
        )

        view = PostViewSet.as_view({"patch": "reply_detail"})
        response = view(
            self._request(
                "patch",
                self.owner,
                f"/api/v1/community/posts/{self.visible_post.id}/replies/{own_reply.id}/",
                {"parent_reply": other_reply.id},
            ),
            pk=self.visible_post.id,
            reply_id=own_reply.id,
        )

        self.assertEqual(response.status_code, 400)
        own_reply.refresh_from_db()
        self.assertIsNone(own_reply.parent_reply_id)


class TestCommunityUserBlockGuard(CommunityHardeningFixture):
    def test_block_user_rejects_user_outside_current_tenant(self):
        other_user = User.objects.create_user(
            username="comm_other_user",
            password="pw1234",
            tenant=self.other_tenant,
            name="Other",
        )
        TenantMembership.ensure_active(tenant=self.other_tenant, user=other_user, role="student")

        from apps.domains.community.api.views.admin_views import CommunityUserBlockView

        view = CommunityUserBlockView.as_view()
        response = view(
            self._request(
                "post",
                self.owner,
                "/api/v1/community/admin/user-blocks/",
                {"user_id": other_user.id, "reason": "outside tenant"},
            )
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            CommunityUserBlock.objects.filter(tenant=self.tenant, user=other_user).exists()
        )


class TestCommunityAdminListLimits(CommunityHardeningFixture):
    def test_admin_page_size_is_capped(self):
        PostEntity.objects.bulk_create(
            [
                PostEntity(
                    tenant=self.tenant,
                    post_type="qna",
                    title=f"Post {idx}",
                    content="body",
                    author_role="staff",
                    status="published",
                )
                for idx in range(120)
            ]
        )
        view = AdminPostViewSet.as_view({"get": "list"})
        response = view(
            self._request(
                "get",
                self.owner,
                "/api/v1/community/admin/posts/?page_size=500",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 122)
        self.assertEqual(len(response.data["results"]), 100)


class TestCommunityListQueryShape(CommunityHardeningFixture):
    def test_board_list_annotates_is_liked_without_per_row_like_queries(self):
        extra_posts = [
            PostEntity.objects.create(
                tenant=self.tenant,
                post_type="board",
                title=f"Post {idx}",
                content="body",
                author_role="staff",
                status="published",
            )
            for idx in range(12)
        ]
        for post in extra_posts[:3]:
            PostLike.objects.create(tenant=self.tenant, post=post, user=self.owner)

        view = PostViewSet.as_view({"get": "board"})
        request = self._request("get", self.owner, "/api/v1/community/posts/board/?page_size=20")

        with CaptureQueriesContext(connection) as captured:
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["results"]), 12)
        like_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "community_post_like" in query["sql"]
        ]
        self.assertLessEqual(len(like_queries), 2)
        liked_flags = [row["is_liked"] for row in response.data["results"]]
        self.assertEqual(sum(1 for flag in liked_flags if flag), 3)
