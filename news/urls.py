from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path("api/conversations/", views.conversation_list, name="conversation_list"),
    path(
        "api/conversations/<int:conversation_id>/",
        views.conversation_detail,
        name="conversation_detail",
    ),
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/register/", views.register, name="register"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("topic/<int:topic_id>/", views.topic_detail, name="topic_detail"),
    path("topic/<int:topic_id>/refresh/", views.refresh_topic, name="refresh_topic"),
]