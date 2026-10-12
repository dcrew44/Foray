from django.urls import path

from apps.chat import views as chat_views
from foray.api import api

urlpatterns = [
    # The one async endpoint is a plain Django view (§7.3), mounted before the ninja API.
    path("api/chat", chat_views.chat_view),
    path("api/", api.urls),
]
