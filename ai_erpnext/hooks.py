app_name = "ai_erpnext"
app_title = "AI"
app_publisher = ".."
app_description = ".."
app_email = "anshum25506@gmail.com"
app_license = "mit"

# Apps
# ------------------

# The app maps documents to ERPNext DocTypes (Sales Invoice, Customer, Item, ...).
required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "ai_erpnext",
# 		"logo": "/assets/ai_erpnext/logo.png",
# 		"title": "AI",
# 		"route": "/ai_erpnext",
# 		"has_permission": "ai_erpnext.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ai_erpnext/css/ai_erpnext.css"
# Bundled (content-hashed URL) so browsers never run a stale cached copy.
app_include_js = "ai_erpnext.bundle.js"

# include js, css files in header of web template
# web_include_css = "/assets/ai_erpnext/css/ai_erpnext.css"
# web_include_js = "/assets/ai_erpnext/js/ai_erpnext.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ai_erpnext/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ai_erpnext/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "ai_erpnext.utils.jinja_methods",
# 	"filters": "ai_erpnext.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ai_erpnext.install.before_install"
# after_install = "ai_erpnext.install.after_install"
after_install = "ai_erpnext.setup_doctypes.after_install"

# Uninstallation
# ------------

# before_uninstall = "ai_erpnext.uninstall.before_uninstall"
# after_uninstall = "ai_erpnext.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ai_erpnext.utils.before_app_install"
# after_app_install = "ai_erpnext.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ai_erpnext.utils.before_app_uninstall"
# after_app_uninstall = "ai_erpnext.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "ai_erpnext.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ai_erpnext.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["ai_erpnext.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"AI Document Entry": {
		"validate": "ai_erpnext.pipeline.controller.validate_ai_document",
		"before_save": "ai_erpnext.pipeline.controller.process_ai_document",
	},
	# Local checks only (missing fallback key, cap, unsupported provider); never calls a provider.
	"AI Provider Settings": {"validate": "ai_erpnext.ai.providers.validate_settings"},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"ai_erpnext.tasks.all"
# 	],
# 	"daily": [
# 		"ai_erpnext.tasks.daily"
# 	],
# 	"hourly": [
# 		"ai_erpnext.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ai_erpnext.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ai_erpnext.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "ai_erpnext.install.before_tests"
before_tests = "ai_erpnext.tests.bootstrap.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ai_erpnext.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ai_erpnext.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ai_erpnext.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ai_erpnext.utils.before_request"]
# after_request = ["ai_erpnext.utils.after_request"]

# Job Events
# ----------
# before_job = ["ai_erpnext.utils.before_job"]
# after_job = ["ai_erpnext.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"ai_erpnext.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# AI Document Entry is a custom (DB) DocType, and Frappe ignores doctype_js for custom
# DocTypes, so its form script (ai_document_entry.js) is loaded via app_include_js above.

after_migrate = ["ai_erpnext.setup_doctypes.after_migrate"]

# Close and trim expired assistant conversations (runs when the scheduler is enabled).
scheduler_events = {"daily": ["ai_erpnext.chat.session.cleanup_expired_sessions"]}

# Other apps may add AI document definitions: list of modules calling
# ai_erpnext.registry.document_registry.register() on import.
ai_document_definitions = []
