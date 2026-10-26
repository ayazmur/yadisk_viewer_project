from django.urls import path, include
from . import views

app_name = 'viewer'

urlpatterns = [
    path('', views.index, name='index'),
    path('download/<path:file_path>', views.download_file, name='download_file'),
    path('download_zip/', views.download_zip, name='download_zip'),
    path('download_all_zip/', views.download_all_zip, name='download_all_zip'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
]

urlpatterns += [
    path('oauth/', include('social_django.urls', namespace='social')),
]
