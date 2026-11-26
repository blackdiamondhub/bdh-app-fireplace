from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="fireplace_index"),
    path("api/state/", views.api_state, name="fireplace_state"),
    path("api/act/", views.api_act, name="fireplace_act"),
    path("api/looks/", views.api_looks, name="fireplace_looks"),
]
