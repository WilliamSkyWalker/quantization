"""
Django settings.
Reads database config from .env via services/config.py.
"""
import os
import time
import logging
from pathlib import Path

# 日志时间使用本地时区（Asia/Shanghai）
logging.Formatter.converter = time.localtime

# 项目根目录（quantization/）
BASE_DIR = Path(__file__).resolve().parent.parent

# Import service layer config
from services import config as qs_settings

SECRET_KEY = 'django-insecure-quant-local-dev-only-not-for-production'

DEBUG = True

ALLOWED_HOSTS = ['*']

TIME_ZONE = 'Asia/Shanghai'
USE_TZ = True

INSTALLED_APPS = [
    'daphne',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'api',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'core.urls'

FRONTEND_DIST = BASE_DIR / 'frontend' / 'dist'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [FRONTEND_DIST],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

ASGI_APPLICATION = 'core.asgi.application'

# No Django database needed - we use SQLAlchemy from services layer
DATABASES = {}

# CORS - allow Vue dev server
CORS_ALLOW_ALL_ORIGINS = True

# DRF settings
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'core.renderers.UTF8JSONRenderer',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'UNAUTHENTICATED_USER': None,
}

# Channels
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

STATIC_URL = '/static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Logging - show INFO level for our services
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'services': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'api': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'tasks': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# Expose service layer settings for use in views
QUANT_SETTINGS = qs_settings
