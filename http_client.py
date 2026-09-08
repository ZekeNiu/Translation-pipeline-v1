"""Bounded network retries, cancellation and safe, useful error messages."""
from __future__ import annotations
import threading
import time
from email.utils import parsedate_to_datetime
import requests
from task_state import check_cancel, interruptible_wait


class RemoteError(RuntimeError):
    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status, self.transient = status, transient


_local = threading.local()


def thread_session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def request(session, method, url, *, cancel_event=None, attempts=3, **kwargs):
    body = kwargs.get("data")
    position = body.tell() if hasattr(body, "seek") else None
    for attempt in range(attempts):
        check_cancel(cancel_event)
        if position is not None:
            body.seek(position)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt + 1 == attempts:
                raise RemoteError("网络连接中断或超时；进度已保留，可稍后继续。", transient=True) from exc
            interruptible_wait(2 ** attempt, cancel_event)
            continue
        status = response.status_code
        if 200 <= status < 300:
            return response
        transient = status == 429 or status >= 500
        if transient and attempt + 1 < attempts:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except (ValueError, TypeError):
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 2 ** attempt
            response.close()
            interruptible_wait(min(60, max(1, delay)), cancel_event)
            continue
        response.close()
        labels = {401: "Key 无效或已过期，请更新后继续。", 403: "服务拒绝访问，请检查 Key 权限或额度。",
                  413: "文件超出服务限制；Office 文件请先导出为 PDF。", 429: "服务限流，请稍后继续。"}
        raise RemoteError(labels.get(status, f"服务请求失败（HTTP {status}），进度已保留。"), status, transient)
