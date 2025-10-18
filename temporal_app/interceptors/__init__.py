"""Interceptor package for cross-cutting concerns (Lark notifications, metrics, etc)."""

from temporal_app.interceptors.lark.notifier import LarkNotifierInterceptor

__all__ = [
    "LarkNotifierInterceptor",
]


