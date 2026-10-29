from django.urls import path
from . import views

app_name = 'viewer'

urlpatterns = [
    path('', views.index, name='index'),
    path('download/<path:file_path>', views.download_file, name='download_file'),
    path('preview/<path:file_path>', views.preview_file, name='preview_file'),
    path('download_zip/', views.download_zip, name='download_zip'),
    path('download_all_zip/', views.download_all_zip, name='download_all_zip'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('upload/', views.upload_file, name='upload_file'),
    path('upload/init/', views.upload_init, name='upload_init'),
    path('upload/chunk/', views.upload_chunk, name='upload_chunk'),
    path('upload/complete/', views.upload_complete, name='upload_complete'),
    path('upload/abort/', views.upload_abort, name='upload_abort'),
    path('create_folder/', views.create_folder, name='create_folder'),
]
