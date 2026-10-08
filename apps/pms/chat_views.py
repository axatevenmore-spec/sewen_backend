"""Messenger endpoints, nested under the project like stages and documents.

    GET    /pms/projects/{id}/conversations/                         list (+ unread)
    POST   /pms/projects/{id}/conversations/                         start a direct chat
    GET    /pms/projects/{id}/conversations/{cid}/messages/          page / poll
    POST   /pms/projects/{id}/conversations/{cid}/messages/          send
    PATCH  /pms/projects/{id}/conversations/{cid}/messages/{mid}/    edit own
    DELETE /pms/projects/{id}/conversations/{cid}/messages/{mid}/    delete own
    POST   /pms/projects/{id}/conversations/{cid}/read/              mark read
    GET    /pms/projects/{id}/messages/search/?q=                    search
    GET    /pms/projects/{id}/team/                                  the team
    POST   /pms/projects/{id}/team/                                  add a member
    DELETE /pms/projects/{id}/team/{memberId}/                       remove one
    GET    /pms/my-chats/                                            my projects

Attachments are uploaded first through the ordinary ``/files/`` flow, and the
committed file ids are sent as ``attachmentIds`` on the message -- the same
shape stage documents use.
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission

from . import chat, team
from .serializers import (
    EditMessageSerializer,
    MessageSerializer,
    PostMessageSerializer,
    StartConversationSerializer,
)

#: No permission at the route: anyone working on the project chats on it,
#: PMS access or not (a welder added to the team has no ``view_pms``). Which
#: project and which conversations the caller can reach is decided per project
#: by ``chat.ChatScope`` -- outsiders get a 404.
CHAT_PERMISSIONS = {
    "conversations": [],
    "conversation_messages": [],
    "conversation_message_detail": [],
    "conversation_read": [],
    "search_messages": [],
    # Team list: read by its members, changed by the PM (checked in ``team``).
    "team": [],
    "team_member": [],
}


class ProjectChatMixin:
    """Mixed into ``ProjectViewSet``; relies on its ``get_object``."""

    def _chat_scope(self):
        project = self.get_object()
        scope = chat.ChatScope(project, self.request.user)
        team.require_view(self.request.user, project, scope)
        return scope

    @action(detail=True, methods=["get", "post"], url_path="team")
    def team(self, request, pk=None):
        project = self.get_object()
        if request.method == "POST":
            team.require_manage(request.user, project)
            team.add_member(
                project,
                request.user,
                request.data.get("userId"),
                request.data.get("departmentId") or None,
            )
        else:
            team.require_view(request.user, project)
        return Response(team.team_payload(project, request.user))

    @action(detail=True, methods=["delete"], url_path=r"team/(?P<member_id>[^/.]+)")
    def team_member(self, request, pk=None, member_id=None):
        project = self.get_object()
        team.require_manage(request.user, project)
        team.remove_member(project, request.user, member_id)
        return Response(team.team_payload(project, request.user))

    def _message_data(self, messages, many=False):
        return MessageSerializer(
            messages, many=many, context=self.get_serializer_context()
        ).data

    @action(detail=True, methods=["get", "post"], url_path="conversations")
    def conversations(self, request, pk=None):
        scope = self._chat_scope()
        if request.method == "POST":
            serializer = StartConversationSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            conversation, created = chat.start_direct(
                scope, serializer.validated_data["userId"]
            )
            rows, _ = chat.list_conversations(scope)
            row = next(r for r in rows if r["id"] == str(conversation.id))
            return Response(
                row, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
            )

        rows, aggregates = chat.list_conversations(scope)
        return Response(envelope(rows, aggregates=aggregates))

    @action(
        detail=True,
        methods=["get", "post"],
        url_path=r"conversations/(?P<conversation_id>[^/.]+)/messages",
    )
    def conversation_messages(self, request, pk=None, conversation_id=None):
        scope = self._chat_scope()
        conversation = chat.get_conversation(scope, conversation_id)

        if request.method == "POST":
            serializer = PostMessageSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            message = chat.post_message(scope, conversation, serializer.validated_data)
            return Response(self._message_data(message), status=status.HTTP_201_CREATED)

        params = request.query_params
        rows, aggregates = chat.list_messages(
            conversation,
            before=params.get("before"),
            since=params.get("since"),
            limit=params.get("limit"),
        )
        return Response(envelope(self._message_data(rows, many=True), aggregates=aggregates))

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"conversations/(?P<conversation_id>[^/.]+)/messages/(?P<message_id>[^/.]+)",
    )
    def conversation_message_detail(self, request, pk=None, conversation_id=None, message_id=None):
        scope = self._chat_scope()
        conversation = chat.get_conversation(scope, conversation_id)
        if request.method == "DELETE":
            message = chat.delete_message(scope, conversation, message_id)
            return Response(self._message_data(message))

        serializer = EditMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = chat.edit_message(scope, conversation, message_id, serializer.validated_data)
        return Response(self._message_data(message))

    @action(
        detail=True,
        methods=["post"],
        url_path=r"conversations/(?P<conversation_id>[^/.]+)/read",
    )
    def conversation_read(self, request, pk=None, conversation_id=None):
        scope = self._chat_scope()
        conversation = chat.get_conversation(scope, conversation_id)
        read_at = chat.mark_read(scope, conversation)
        return Response({"conversationId": str(conversation.id), "readAt": chat.iso_time(read_at)})

    @action(detail=True, methods=["get"], url_path="messages/search")
    def search_messages(self, request, pk=None):
        scope = self._chat_scope()
        rows = chat.search_messages(
            scope, request.query_params.get("q"), request.query_params.get("conversationId")
        )
        return Response(envelope(self._message_data(rows, many=True)))


class MyProjectChatsView(APIView):
    """``GET /pms/my-chats/`` -- the projects I work on, for the Project Chats
    page every employee has (no PMS access needed)."""

    permission_classes = [HasModulePermission]
    required_permissions = []

    def get(self, request):
        return Response(envelope(team.my_chats(request.user)))
