from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    urlpatterns = [
        re_path(r"^execute/$", views.TaskReport.as_view(), name="execute"),
        re_path(
            r"^execute/data/$",
            views.data_management,
            name="execute_data_management",
        ),
        re_path(
            r"^execute/planningboard/$",
            views.PlanEdit.as_view(),
            name="execute_planningboard",
        ),
        re_path(
            r"^execute/autoresolve/$",
            views.auto_resolve_conflicts,
            name="execute_autoresolve",
        ),
        re_path(
            r"^execute/gantt/embed/$",
            views.GanttEmbed.as_view(),
            name="execute_gantt_embed",
        ),
        re_path(
            r"^execute/logccAPPS/(.+)/$",
            views.logfile,
            name="execute_view_log",
        ),
        re_path(
            r"^execute/launch/(.+)/$",
            views.LaunchTask,
            name="execute_launch",
        ),
        re_path(
            r"^execute/cancel/(.+)/$",
            views.CancelTask,
            name="execute_cancel",
        ),
        re_path(
            r"^execute/logdownload/(.+)/$",
            views.DownloadLogFile,
            name="execute_download_log",
        ),
        re_path(
            r"^execute/logdelete/(.+)/$",
            views.DeleteLogFile,
            name="execute_delete_log",
        ),
        re_path(r"^execute/api/(.+)/$", views.APITask, name="execute_api"),
        # Public CCPL entry: import 13-table payload (+ optional runplan).
        # Path kept as /api/ccpl/plan/ (replaces former /api/ccpl/import/).
        re_path(
            r"^api/ccpl/plan/$",
            views.CCPLPlanAPI,
            name="ccpl_plan_api",
        ),
        re_path(
            r"^execute/uploadtofolder/(.+)/$",
            views.FileManager.uploadFiletoFolder,
            name="copy_file_to_folder",
        ),
        re_path(
            r"^execute/downloadfromfolder/(.+)/(.+)/$",
            views.FileManager.downloadFilefromFolder,
            name="download_file_from_folder",
        ),
        re_path(
            r"^execute/downloadfromfolder/(.+)/$",
            views.FileManager.downloadFilefromFolder,
            name="download_all_files_from_folder",
        ),
        re_path(
            r"^execute/deletefromfolder/(.+)/(.+)/$",
            views.FileManager.deleteFilefromFolder,
            name="delete_file_from_folder",
        ),
        re_path(
            r"^execute/exports/$",
            views.exports,
            name="exports",
        ),
        re_path(
            r"^execute/scheduletasks/$",
            views.scheduletasks,
            name="scheduletasks",
        ),
        re_path(
            r"^execute/scenario_add/$",
            views.scenario_add,
            name="scenario_add",
        ),
        re_path(
            r"^execute/scenario_delete/$",
            views.scenario_delete,
            name="scenario_delete",
        ),
    ]
