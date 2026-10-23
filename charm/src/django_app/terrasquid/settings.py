"""Django settings for the Terrasquid project."""

import os

import dj_database_url

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if os.environ.get("DEBUG", "false").lower() == "true":
        SECRET_KEY = "django-insecure-dev-only-change-in-production"
    else:
        raise RuntimeError("SECRET_KEY environment variable must be set.")

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django_prometheus",
    "terrasquid.apps.TerrasquidConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_api_key",
    "drf_spectacular",
    "terrasquid.api",
]

MIDDLEWARE = [
    "terrasquid.middleware.LocalhostMetricsMiddleware",
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"trace_context": {"()": "terrasquid.telemetry.TraceContextFilter"}},
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name} trace_id={trace_id} span_id={span_id} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["trace_context"],
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}

ROOT_URLCONF = "terrasquid.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "terrasquid.wsgi.application"

_default_db = "sqlite:///{}".format(os.path.join(BASE_DIR, "db.sqlite3"))
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL", _default_db),
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework_api_key.permissions.HasAPIKey"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Terrasquid API",
    "DESCRIPTION": "REST API for the Terrasquid (Squid-as-a-Service) charm.",
    "VERSION": "1.0.0",
}

JUJU_UNIT_NAME = os.environ.get("JUJU_UNIT_NAME", "squid-as-a-service/0")

TERRASQUID_STATUS_FILE = os.environ.get(
    "TERRASQUID_STATUS_FILE",
    "/var/lib/terrasquid/status.json",
)

SQUID_PORT = int(os.environ.get("SQUID_PORT", "3128"))
SQUID_PREPEND_CONFIG = os.environ.get("SQUID_PREPEND_CONFIG", "")
SQUID_APPEND_CONFIG = os.environ.get("SQUID_APPEND_CONFIG", "")
SQUID_DEFAULT_DENY = os.environ.get("SQUID_DEFAULT_DENY", "True").lower() not in ("false", "0", "")
SQUID_CONF_PATH = os.environ.get("SQUID_CONF_PATH", "/etc/squid/squid.conf")
SQUID_BINARY = os.environ.get("SQUID_BINARY", "/usr/sbin/squid")
SQUID_PINNED_CONFIG_VERSION = int(os.environ.get("SQUID_PINNED_CONFIG_VERSION", "0"))
