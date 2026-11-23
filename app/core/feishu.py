# -*- coding: utf-8 -*-
"""飞书：token、多维表格导出、连接测试。"""
import os
import json
import time
import urllib.request
import urllib.error


class FeishuClient:
    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expire = 0

    def _req(self, method, url, data=None, headers=None):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            import os
            os.environ.pop(k, None)
        h = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            h.update(headers)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"飞书API错误 ({e.code}): {e.read().decode('utf-8', errors='replace')[:200]}")

    def _raw(self, method, url, body_bytes, headers):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            import os
            os.environ.pop(k, None)
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"飞书API错误 ({e.code}): {e.read().decode('utf-8', errors='replace')[:200]}")

    def get_token(self):
        if self._token and time.time() < self._expire - 60:
            return self._token
        r = self._req("POST", f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
                      {"app_id": self.app_id, "app_secret": self.app_secret})
        if r.get("code") != 0:
            raise RuntimeError(f"获取token失败: {r.get('msg')}")
        self._token = r["tenant_access_token"]
        self._expire = time.time() + r.get("expire", 7200)
        return self._token

    def auth_headers(self):
        return {"Authorization": "Bearer " + self.get_token(),
                "Content-Type": "application/json; charset=utf-8"}

    def list_fields(self, app_token, table_id):
        r = self._req("GET", f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                      headers=self.auth_headers())
        if r.get("code") != 0:
            raise RuntimeError(f"获取字段失败: {r.get('msg')}")
        return r.get("data", {}).get("items", [])

    def create_field(self, app_token, table_id, name, ftype=1):
        r = self._req("POST", f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                      {"field_name": name, "type": ftype}, headers=self.auth_headers())
        if r.get("code") != 0 and "FieldNameExist" not in str(r.get("msg", "")):
            raise RuntimeError(f"创建字段失败: {r.get('msg')}")

    def batch_create(self, app_token, table_id, records):
        url = f"{self.BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        result = None
        for i in range(0, len(records), 500):
            r = self._req("POST", url, {"records": records[i:i + 500]}, headers=self.auth_headers())
            if r.get("code") != 0:
                raise RuntimeError(f"写入记录失败: {r.get('msg')}")
            result = r
            time.sleep(0.3)
        return {"total": len(records)}

    def upload_file(self, path, app_token):
        import uuid
        url = f"{self.BASE_URL}/drive/v1/medias/upload_all"
        with open(path, "rb") as f:
            data = f.read()
        boundary = uuid.uuid4().hex
        body = []
        fname = os.path.basename(path)
        for k, v in [("file_name", fname), ("parent_type", "bitable_file"),
                     ("parent_node", app_token), ("size", str(len(data))), ("extra", "")]:
            body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                    f'Content-Type: application/octet-stream\r\n\r\n'.encode())
        body.append(data)
        body.append(f'\r\n--{boundary}--\r\n'.encode())
        h = self.auth_headers()
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        r = self._raw("POST", url, b"".join(body), h)
        if r.get("code") != 0:
            raise RuntimeError(f"上传文件失败: {r.get('msg')}")
        return r.get("data", {}).get("file_token")


def parse_bitable_url(u):
    """从多维表格链接解析 app_token 与 table_id"""
    import re
    u = u or ""
    m = re.search(r"/base/([A-Za-z0-9]+)", u)
    t = re.search(r"[?&]table=([A-Za-z0-9]+)", u)
    return (m.group(1) if m else "", t.group(1) if t else "")


def ensure_fields(client, app_token, table_id):
    required = [("视频ID", 1), ("标题", 1), ("链接", 1), ("时长(秒)", 2),
                ("点赞", 2), ("评论", 2), ("收藏", 2), ("转发", 2),
                ("主播", 1), ("粉丝", 2), ("一句话总结", 1), ("适合场景", 1),
                ("核心结构", 1), ("画面分析", 1), ("值得学习", 1),
                ("改进建议", 1), ("分析时间", 1), ("画面图片", 17)]
    existing = {f.get("field_name") for f in client.list_fields(app_token, table_id)}
    failed = []
    for name, ftype in required:
        if name not in existing:
            try:
                client.create_field(app_token, table_id, name, ftype)
            except Exception:
                failed.append(name)
    if failed:
        raise RuntimeError(f"创建字段失败: {', '.join(failed[:6])}（请检查 base:table:update 权限并发布）")


def export_report(report, feishu_conf):
    """导出单条报告到多维表格"""
    app_id = (feishu_conf.get("app_id") or "").strip()
    secret = (feishu_conf.get("app_secret") or "").strip()
    app_token = (feishu_conf.get("app_token") or "").strip()
    table_id = (feishu_conf.get("table_id") or "").strip()
    if not (app_id and secret and app_token and table_id):
        raise RuntimeError("未配置飞书（App ID / Secret / app_token / table_id）")
    client = FeishuClient(app_id, secret)
    ensure_fields(client, app_token, table_id)

    import os
    stats = report.get("stats") or {}
    analysis = report.get("analysis") or {}
    vision = report.get("vision") or []
    vision_text = "\n".join(f"[{v.get('time', 0)}秒] {v.get('analysis', '')}" for v in vision)
    hl = "\n".join(f"• {h.get('point')}: {h.get('how')} → {h.get('use')}"
                   for h in (analysis.get("highlights") or []))
    imp = "\n".join(f"• {i.get('point')}: {i.get('problem')} → {i.get('fix')}"
                    for i in (analysis.get("improvements") or []))

    def num(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    fields = {
        "视频ID": str(report.get("video_id") or ""),
        "标题": str(report.get("title") or ""),
        "链接": report.get("url") or "",
        "时长(秒)": num(report.get("duration")),
        "点赞": num(stats.get("like")), "评论": num(stats.get("comment")),
        "收藏": num(stats.get("collect")), "转发": num(stats.get("share")),
        "主播": str(stats.get("nickname") or stats.get("channel_name") or ""),
        "粉丝": num(stats.get("fans")),
        "一句话总结": str(analysis.get("summary") or ""),
        "适合场景": str(analysis.get("scenes") or ""),
        "核心结构": str(analysis.get("structure") or ""),
        "画面分析": vision_text,
        "值得学习": hl,
        "改进建议": imp,
        "分析时间": time.strftime("%Y-%m-%d %H:%M"),
    }
    client.batch_create(app_token, table_id, [{"fields": fields}])
    return {"ok": True, "message": "已导出到飞书多维表格"}


def test_connection(feishu_conf):
    try:
        app_id = (feishu_conf.get("app_id") or "").strip()
        secret = (feishu_conf.get("app_secret") or "").strip()
        app_token = (feishu_conf.get("app_token") or "").strip()
        table_id = (feishu_conf.get("table_id") or "").strip()
        if not (app_id and secret):
            return {"ok": False, "message": "未配置 App ID / Secret"}
        c = FeishuClient(app_id, secret)
        c.get_token()
        if not (app_token and table_id):
            return {"ok": False, "message": "凭证正常，但未配置 app_token / table_id"}
        c.list_fields(app_token, table_id)
        return {"ok": True, "message": "连接成功（凭证 + 表格权限均正常）"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}
