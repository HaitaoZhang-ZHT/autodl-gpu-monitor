#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AutoDL 空闲 GPU 监控器
======================

监控 AutoDL 算力商城中「指定地区 + 指定 GPU 型号」的空闲资源，
一旦出现就本地提示 + 蜂鸣 + 微信(Server酱) / 自定义 Webhook 推送。

- 仅依赖 Python 标准库，无需 pip install
- 地区列表从 AutoDL 公开接口实时拉取，不写死、不会过期
- 默认「只监控不购买」，不会误下单扣费

用法
----
    python autodl_monitor.py                     # 按 config.json 持续监控
    python autodl_monitor.py --once              # 只查一次
    python autodl_monitor.py --list-regions      # 列出当前所有地区
    python autodl_monitor.py --list-gpus         # 列出各地区在售 GPU 型号
    python autodl_monitor.py --check             # 检查 token 是否有效
    python autodl_monitor.py --login --phone 13xxxxxxxxxx   # 手机号密码登录,自动写回 config
    python autodl_monitor.py --regions 北京A区 --gpus "RTX 4090" --min-idle 1

作者备注：接口与字段均对照 AutoDL 线上前端实际调用方式确定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "monitor.log")
TOKEN_FILE = os.path.join(HERE, "token.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 前端把 on_line 属于这 4 个值的主机视为「离线」，空闲数强制按 0 算
OFFLINE_ON_LINE = {3, 4, 5, 6}

DEFAULT_CONFIG = {
    "api_base": "https://api.autodl.com",
    "authorization": "",
    "regions": [],
    "gpus": [],
    "min_idle_gpu": 1,
    "gpu_match": "exact",
    "interval_seconds": 30,
    "timeout_seconds": 20,
    "page_size": 100,
    "max_pages": 5,
    "notify_on_change_only": True,
    "cooldown_seconds": 180,
    "open_browser_on_new": True,
    "market_url": "https://www.autodl.com/market/list/",
    "notification": {
        "sendkey": "",
        "webhook_url": "",
        "resend_api_key": "",
        "email": {
            "enabled": True,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "use_ssl": True,
            "smtp_user": "",
            "from": "",
            "to": [],
            "subject_prefix": "[AutoDL监控] ",
            "password": "",
            "cooldown_seconds": 900
        },
    },
}


# ------------------------------------------------------------------ 基础工具
def fix_stdout():
    """让中文在控制台里正确显示。

    Windows 控制台默认 GBK(cp936)。如果强行按 UTF-8 写，cmd/PowerShell 窗口会显示乱码；
    正确做法是把控制台代码页切到 65001 再写 UTF-8。若切换失败，则保留系统原生编码
    （GBK 也能正常显示中文），只把 errors 放宽，避免个别字符直接抛异常。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            if os.name == "nt" and stream.isatty():
                import ctypes
                if ctypes.windll.kernel32.GetConsoleOutputCP() != 65001:
                    # 切不动就沿用系统编码（GBK 显示中文没问题）
                    stream.reconfigure(errors="replace")
                    continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, level="INFO"):
    line = "[%s] [%s] %s" % (now_str(), level, msg)
    try:
        print(line, flush=True)
    except Exception:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_config(path=CONFIG_PATH):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                notif = user.get("notification")
                cfg.update(user)
                if isinstance(notif, dict):
                    base_notif = dict(DEFAULT_CONFIG["notification"])
                    base_notif.update(notif)
                    cfg["notification"] = base_notif
        except Exception as exc:
            log("config.json 解析失败，改用默认配置: %s" % exc, "WARN")
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


def resolve_token(cfg, cli_token=None):
    """token 优先级：命令行 > 环境变量 > config.json > token.txt"""
    for cand in (cli_token, os.environ.get("AUTODL_TOKEN"),
                 cfg.get("authorization")):
        if cand and cand.strip():
            return cand.strip()
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
            if val:
                return val
        except Exception:
            pass
    return ""


# ------------------------------------------------------------------ HTTP
def http_json(url, payload=None, headers=None, timeout=20, form=False):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=hdrs, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("响应不是合法 JSON: %s" % text[:300])


# ------------------------------------------------------------------ AutoDL 客户端
class AutoDL:
    def __init__(self, token=None, base="https://api.autodl.com", timeout=20):
        self.token = (token or "").strip()
        self.base = (base or DEFAULT_CONFIG["api_base"]).rstrip("/")
        self.timeout = timeout

    def _headers(self):
        return {"Authorization": self.token} if self.token else {}

    def _check(self, resp, what):
        code = resp.get("code")
        if code != "Success":
            msg = resp.get("msg") or ""
            if code == "AuthorizeFailed":
                raise RuntimeError(
                    "%s 需要有效 token（登录已过期或未配置）。\n"
                    "  获取方式见 README：控制台→设置→开发者token，或用 --login 重新登录。" % what)
            raise RuntimeError("%s 接口返回异常: code=%s msg=%s" % (what, code, msg))
        return resp

    def regions(self):
        """GET /api/v1/region/list —— 公开接口，无需登录。"""
        resp = http_json(self.base + "/api/v1/region/list",
                         headers=self._headers(), timeout=self.timeout)
        return self._check(resp, "地区列表").get("data") or []

    def region_gpu_types(self, region_signs):
        """POST /api/v1/machine/region/gpu_type —— 某地区在售的 GPU 型号及库存。"""
        resp = http_json(self.base + "/api/v1/machine/region/gpu_type",
                         payload={"region_sign_list": list(region_signs)},
                         headers=self._headers(), timeout=self.timeout)
        return self._check(resp, "地区GPU型号").get("data")

    def search(self, region_signs, page_index=1, page_size=100, min_idle=0):
        """POST /api/v1/machine/search —— 算力商城机器列表（空闲数 gpu_idle_num）。"""
        body = {
            "charge_type": "payg",
            "region_sign": "",
            "region_sign_list": list(region_signs),
            "gpu_type_name": [],
            "machine_tag_name": [],
            "gpu_idle_num": int(min_idle or 0),
            "mount_net_disk": False,
            "cpu_arch": [],
            "chip_corp": [],
            "instance_disk_size_order": "",
            "date_range": "",
            "date_from": "",
            "date_to": "",
            "page_index": int(page_index),
            "page_size": int(page_size),
            "pay_price_order": "",
            "gpu_idle_type": "",
            "default_order": True,
        }
        resp = http_json(self.base + "/api/v1/machine/search",
                         payload=body, headers=self._headers(), timeout=self.timeout)
        return self._check(resp, "机器列表")

    def login(self, phone, password):
        """手机号 + 密码登录，返回 token。"""
        sha1 = hashlib.sha1(str(password).encode("utf-8")).hexdigest()
        resp = http_json(self.base + "/api/v1/new_login",
                         payload={"phone": str(phone), "password": sha1,
                                  "phone_area": "+86"},
                         headers=self._headers(), timeout=self.timeout)
        if resp.get("code") != "Success":
            raise RuntimeError("登录失败: %s %s" % (resp.get("code"), resp.get("msg")))
        ticket = (resp.get("data") or {}).get("ticket")
        if not ticket:
            raise RuntimeError("登录未返回 ticket: %s" % resp.get("msg"))
        resp2 = http_json(self.base + "/api/v1/passport",
                          payload={"ticket": ticket},
                          headers=self._headers(), timeout=self.timeout)
        if resp2.get("code") != "Success":
            raise RuntimeError("换取 token 失败: %s %s" % (resp2.get("code"), resp2.get("msg")))
        token = (resp2.get("data") or {}).get("token")
        if not token:
            raise RuntimeError("未取得 token")
        return token


# ------------------------------------------------------------------ 解析
def extract_list(resp):
    """从 machine/search 响应里取出机器列表与总数（兼容多种外壳）。"""
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("list", "machine_list", "machines", "items", "rows"):
            val = data.get(key)
            if isinstance(val, list):
                total = data.get("result_total") or data.get("total") or len(val)
                try:
                    total = int(total)
                except Exception:
                    total = len(val)
                return val, total
        if isinstance(data.get("data"), list):
            return data["data"], len(data["data"])
    if isinstance(data, list):
        return data, len(data)
    return [], 0


def _first(item, keys, default=None):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def normalize_item(item):
    """把一条机器记录归一化成监控需要的字段。"""
    if not isinstance(item, dict):
        return None

    region_sign = _first(item, ("region_sign", "region"), "") or ""
    region_name = _first(item, ("region_name", "region"), "") or ""

    gpu_name = _first(item, ("gpu_name",), "") or ""
    if not gpu_name:
        gtype = item.get("gpu_type")
        if isinstance(gtype, str):
            gpu_name = gtype
        elif isinstance(gtype, dict):
            gpu_name = gtype.get("gpu_name") or gtype.get("name") or ""
    if not gpu_name:
        ginfo = item.get("gpu_type_info")
        if isinstance(ginfo, dict):
            gpu_name = ginfo.get("gpu_name") or ginfo.get("name") or ""

    try:
        gpu_mem = int(item.get("gpu_memory") or 0)
    except Exception:
        gpu_mem = 0
    try:
        gpu_total = int(_first(item, ("gpu_number", "gpu_all_num", "gpu_total_number"), 0) or 0)
    except Exception:
        gpu_total = 0
    try:
        idle = int(item.get("gpu_idle_num") or 0)
    except Exception:
        idle = 0

    on_line = item.get("on_line")
    max_inst = item.get("max_instance_num")
    binding = item.get("binding_instance_num")

    # 与 AutoDL 前端保持一致：离线机器空闲数按 0 算；配额被占满也按 0 算
    if isinstance(on_line, int) and on_line in OFFLINE_ON_LINE:
        idle = 0
    if isinstance(max_inst, int) and isinstance(binding, int):
        if (max_inst - binding) <= 0:
            idle = 0

    price_raw = item.get("payg_price")
    try:
        price = float(price_raw) / 1000.0  # 接口返回「厘/小时」
    except Exception:
        price = None

    machine_id = _first(item, ("machine_id", "uuid", "id"), "") or ""

    return {
        "machine_id": str(machine_id),
        "region_sign": str(region_sign),
        "region_name": str(region_name),
        "gpu_name": str(gpu_name),
        "gpu_mem_gb": round(gpu_mem / (1024 ** 3), 1) if gpu_mem else None,
        "idle": idle,
        "total": gpu_total,
        "on_line": on_line,
        "price": price,
        "rent_mode": item.get("rent_mode") or "",
        "driver": item.get("driver_version") or "",
        "tag": item.get("machine_tag_name") or "",
    }


def resolve_regions(all_regions, wanted):
    """把用户写的地区（中文名或 region_sign）解析成 [(region_sign, region_name)]。"""
    if not wanted:
        return [(r.get("region_sign"), r.get("region_name")) for r in all_regions]

    out, seen = [], set()
    for raw in wanted:
        text = str(raw).strip()
        if not text:
            continue
        hit = None
        for reg in all_regions:                      # region_sign 精确
            if str(reg.get("region_sign", "")).lower() == text.lower():
                hit = reg
                break
        if hit is None:
            # region_name 精确：同名可能对应多个机房（北京A区 = beijing-A/B/D/E）
            same = [r for r in all_regions if str(r.get("region_name", "")) == text]
            if same:
                for c in same:
                    key = str(c.get("region_sign"))
                    if key not in seen:
                        seen.add(key)
                        out.append((key, c.get("region_name")))
                if len(same) > 1:
                    log("地区 '%s' 覆盖 %d 个机房: %s"
                        % (text, len(same),
                           ", ".join(str(c.get("region_sign")) for c in same)), "INFO")
                continue
        if hit is None:                              # region_name 包含
            cands = [r for r in all_regions if text in str(r.get("region_name", ""))]
            if len(cands) == 1:
                hit = cands[0]
            elif len(cands) > 1:
                log("地区 '%s' 匹配到多个，全部纳入: %s"
                    % (text, ", ".join(str(c.get("region_sign")) for c in cands)), "WARN")
                for c in cands:
                    key = str(c.get("region_sign"))
                    if key not in seen:
                        seen.add(key)
                        out.append((key, c.get("region_name")))
                continue
        if hit is None:
            log("未找到地区 '%s'，已忽略（可用 --list-regions 查看当前地区）" % text, "WARN")
            continue
        key = str(hit.get("region_sign"))
        if key not in seen:
            seen.add(key)
            out.append((key, hit.get("region_name")))
    return out


def normalize_gpu_name(text):
    """忽略大小写、空格、下划线、连字符：'RTX 2080 Ti' == 'RTX2080Ti'。"""
    return re.sub(r"[\s_\-]+", "", str(text or "")).lower()


def resolve_gpu_names(present, wanted, mode="exact"):
    """把用户写的 GPU 型号映射到实际在售型号。返回 None 表示不过滤。

    mode = "exact"  完全同名优先（'RTX 4090' 不会误匹配 'RTX 4090D'）
    mode = "loose"  包含匹配（'RTX 2080 Ti' 会同时匹配 'RTX 2080 Ti *2'）
    """
    names = [str(g).strip() for g in (wanted or []) if str(g).strip()]
    if not names:
        return None
    present = [p for p in present if p]
    norm_map = {}
    for p in present:
        norm_map.setdefault(normalize_gpu_name(p), p)

    allowed = set()
    for name in names:
        nq = normalize_gpu_name(name)
        if mode == "loose":
            cands = [p for p in present if nq in normalize_gpu_name(p)]
        else:
            cands = [norm_map[nq]] if nq in norm_map else []
        if not cands:
            # 没有完全同名的 → 退化为包含匹配，并明确告警
            cands = [p for p in present if nq in normalize_gpu_name(p)]
            if cands:
                log("GPU '%s' 无完全同名型号，已按包含匹配纳入: %s"
                    % (name, ", ".join(sorted(cands))), "WARN")
            else:
                log("当前在售型号里没有 '%s'，将持续等待它出现" % name, "INFO")
        elif mode == "exact":
            near = [p for p in present
                    if p not in cands and nq in normalize_gpu_name(p)]
            if near:
                log("提示：'%s' 另有近似型号未纳入 -> %s（如需一并监控，"
                    "把它写进 gpus，或把 gpu_match 设为 loose）"
                    % (name, ", ".join(sorted(near))), "INFO")
        allowed.update(cands)
    return allowed


def parse_region_gpu_types(data):
    """把 region/gpu_type 的多种返回形态统一成 {型号: 详情}。"""
    out = {}

    def merge(obj):
        if not isinstance(obj, dict):
            return
        name = obj.get("gpu_name") or obj.get("gpu_type")
        if isinstance(name, str) and name:
            out.setdefault(name, obj)
            return
        for key, val in obj.items():
            if isinstance(val, dict):
                out.setdefault(key, val)
            elif isinstance(val, str) and val:
                out.setdefault(val, {})

    if isinstance(data, dict):
        for val in data.values():
            if isinstance(val, list):
                for item in val:
                    merge(item)
            else:
                merge(val)
    elif isinstance(data, list):
        for item in data:
            merge(item)
    return out


# ------------------------------------------------------------------ 通知
def beep(times=3):
    try:
        import winsound
        for _ in range(times):
            winsound.Beep(1500, 400)
            time.sleep(0.2)
    except Exception:
        try:
            sys.stdout.write("\a" * times)
            sys.stdout.flush()
        except Exception:
            pass


def notify_serverchan(sendkey, title, desp):
    url = "https://sctapi.ftqq.com/%s.send" % urllib.parse.quote(sendkey)
    try:
        resp = http_json(url, payload={"title": title, "desp": desp},
                         timeout=15, form=True)
        ok = str(resp.get("code")) == "0"
        return ok, json.dumps(resp, ensure_ascii=False)[:200]
    except Exception as exc:
        return False, str(exc)


def notify_webhook(url, title, content):
    payload = {"title": title, "content": content, "time": now_str()}
    try:
        resp = http_json(url, payload=payload, timeout=15)
        return True, json.dumps(resp, ensure_ascii=False)[:200]
    except Exception as exc:
        return False, str(exc)


def resolve_email_password(cfg):
    """邮箱密码优先级：config > 环境变量 SMTP_QQ_AUTH_CODE > smtp_password.txt"""
    email_cfg = (cfg.get("notification") or {}).get("email") or {}
    pwd = str(email_cfg.get("password") or "").strip()
    if pwd:
        return pwd
    pwd = str(os.environ.get("SMTP_QQ_AUTH_CODE") or "").strip()
    if pwd:
        return pwd
    pwd_file = os.path.join(HERE, "smtp_password.txt")
    if os.path.exists(pwd_file):
        try:
            with open(pwd_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception:
            pass
    return ""


_MAIL_COOLDOWN = {}          # 收件人 -> 最近一次被对方拒收的时间戳
_MAIL_COOLDOWN_SECONDS = 300


def classify_smtp_error(exc):
    """把 SMTP 异常翻译成可读原因 + 处理建议。"""
    text = "%s %s" % (type(exc).__name__, exc)
    if "SMTPRecipientsRefused" in text or "550" in text:
        if "Mailbox unavailable" in text or "access denied" in text:
            return ("收件方拒收（550）",
                    "对方邮箱限流或临时拒收（短时间内重复发信会触发）；"
                    "等几分钟再试，或改用 QQ 收件箱 / Resend")
        return ("收件方拒收（550）", "对方拒收该邮件，检查收件地址是否正确")
    if "SMTPAuthenticationError" in text or "535" in text or "534" in text:
        return ("认证失败",
                "邮箱授权码错误或 SMTP 服务未开启（QQ 邮箱需在设置里开启 SMTP 并使用授权码）")
    if "SMTPServerDisconnected" in text or "ConnectionRefused" in text:
        return ("连接被拒", "SMTP 服务不可达，检查端口 / 网络 / 防火墙")
    if "timed out" in text or "TimeoutError" in text:
        return ("连接超时", "网络不通或被墙，试试 587 STARTTLS 或改用 Resend")
    return ("发送失败", text[:200])


def notify_email(cfg, title, content):
    """通过 SMTP 发邮件（SSL 465 / STARTTLS 均可）。

    **逐个收件人单独发信**：某个收件人被拒（比如 163 限流）不会把其他收件人一起拖掉。
    """
    notif = cfg.get("notification") or {}
    email_cfg = notif.get("email") or {}
    if not email_cfg.get("enabled", True):
        return False, "邮件通知已关闭（notification.email.enabled=false）"

    host = email_cfg.get("smtp_host") or "smtp.qq.com"
    port = int(email_cfg.get("smtp_port") or 465)
    use_ssl = bool(email_cfg.get("use_ssl", True))
    user = str(email_cfg.get("smtp_user") or "").strip()
    pwd = resolve_email_password(cfg)
    sender = str(email_cfg.get("from") or user).strip()
    recipients = email_cfg.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(r).strip() for r in recipients if str(r).strip()]
    cooldown = int(email_cfg.get("cooldown_seconds") or _MAIL_COOLDOWN_SECONDS)

    if not user or not recipients:
        return False, "邮件配置不完整：smtp_user=%s, to=%d 个" % (
            user or "(空)", len(recipients))
    if not pwd:
        return False, ("未找到邮箱密码：请填 notification.email.password，或设环境变量 "
                       "SMTP_QQ_AUTH_CODE，或在程序目录放 smtp_password.txt")

    # 限流冷却：刚被拒过的收件人先跳过，避免每轮轮询都去撞墙
    now = time.time()
    todo, skipped = [], []
    for r in recipients:
        if now - _MAIL_COOLDOWN.get(r, 0) < cooldown:
            skipped.append(r)
        else:
            todo.append(r)
    if not todo:
        return False, ("收件方 %s 均处于限流冷却中（%ds 内不再重试），本次跳过"
                       % (", ".join(skipped), cooldown))

    subject = str(email_cfg.get("subject_prefix") or "") + title
    ok_list, fail_list = [], []
    for r in todo:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender or user
        msg["To"] = r
        msg.set_content(content)
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=25) as srv:
                    srv.login(user, pwd)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=25) as srv:
                    srv.ehlo()
                    srv.starttls()
                    srv.ehlo()
                    srv.login(user, pwd)
                    srv.send_message(msg)
            ok_list.append(r)
            _MAIL_COOLDOWN.pop(r, None)
        except Exception as exc:
            reason, hint = classify_smtp_error(exc)
            if "550" in ("%s" % exc):
                _MAIL_COOLDOWN[r] = time.time()   # 对方限流/拒收，只冷却这一个
            fail_list.append("%s（%s）" % (r, reason))
            log("    邮件未送达 %s —— %s" % (r, reason), "WARN")

    detail = []
    if ok_list:
        detail.append("已发给 " + ", ".join(ok_list))
    if skipped:
        detail.append("跳过(限流冷却 %ds) %s" % (cooldown, ", ".join(skipped)))
    if fail_list:
        detail.append("失败 " + "; ".join(fail_list))
    if ok_list:
        return True, "；".join(detail)
    return False, "；".join(detail) or "没有可发送的收件人"


def notify_resend(cfg, title, content):
    """通过 Resend HTTP API 发邮件（本地不需存密码）。"""
    notif = cfg.get("notification") or {}
    key = str(notif.get("resend_api_key") or "").strip()
    if not key:
        return False, "未配置 resend_api_key"
    email_cfg = notif.get("email") or {}
    sender = str(email_cfg.get("resend_from") or email_cfg.get("from") or "").strip()
    recipients = email_cfg.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(r).strip() for r in recipients if str(r).strip()]
    if not sender or not recipients:
        return False, "Resend 需要 resend_from 与 to"
    payload = {
        "from": sender,
        "to": recipients,
        "subject": str(email_cfg.get("subject_prefix") or "") + title,
        "text": content,
    }
    try:
        resp = http_json("https://api.resend.com/emails", payload=payload,
                         headers={"Authorization": "Bearer " + key}, timeout=25)
        return True, json.dumps(resp, ensure_ascii=False)[:200]
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def send_notifications(cfg, title, content):
    notif = cfg.get("notification") or {}
    sendkey = (notif.get("sendkey") or "").strip()
    webhook = (notif.get("webhook_url") or "").strip()
    resend_key = (notif.get("resend_api_key") or "").strip()
    email_cfg = notif.get("email") or {}
    mail_ready = bool(email_cfg.get("enabled", True)) and bool(
        (email_cfg.get("smtp_user") or email_cfg.get("from")))

    if not sendkey and not webhook and not resend_key and not mail_ready:
        log("未配置任何通知渠道（Server酱 / webhook / 邮件），仅本地提示", "WARN")
        return
    if sendkey:
        ok, detail = notify_serverchan(sendkey, title, content)
        log("Server酱推送 %s %s" % ("成功" if ok else "失败", detail),
            "INFO" if ok else "WARN")
    if webhook:
        ok, detail = notify_webhook(webhook, title, content)
        log("Webhook 推送 %s %s" % ("成功" if ok else "失败", detail),
            "INFO" if ok else "WARN")
    if resend_key:
        ok, detail = notify_resend(cfg, title, content)
        log("Resend 邮件 %s %s" % ("成功" if ok else "失败", detail),
            "INFO" if ok else "WARN")
    elif mail_ready:
        ok, detail = notify_email(cfg, title, content)
        log("邮件通知 %s %s" % ("成功" if ok else "失败", detail),
            "INFO" if ok else "WARN")
    else:
        log("邮件通道未启用（缺 smtp_user / from）", "WARN")


# ------------------------------------------------------------------ 抓取
def fetch_machines(client, region_signs, cfg):
    """拉取所有分页的机器列表并归一化。"""
    page_size = int(cfg.get("page_size") or 100)
    max_pages = int(cfg.get("max_pages") or 5)
    collected, seen = [], set()
    for page in range(1, max_pages + 1):
        resp = client.search(region_signs, page_index=page,
                             page_size=page_size, min_idle=0)
        rows, total = extract_list(resp)
        for row in rows:
            norm = normalize_item(row)
            if norm is None:
                continue
            key = norm["machine_id"] or "%s|%s" % (norm["region_sign"], norm["gpu_name"])
            if key in seen:
                continue
            seen.add(key)
            collected.append(norm)
        if len(rows) < page_size or len(collected) >= total:
            break
        time.sleep(0.3)
    return collected


def describe(item):
    mem = ("%sGB" % item["gpu_mem_gb"]) if item["gpu_mem_gb"] else "?"
    price = ("%.3f 元/时" % item["price"]) if item["price"] is not None else "?"
    return ("%s | %s %s | 空闲 %s/%s | %s | %s"
            % (item["region_name"] or item["region_sign"],
               item["gpu_name"], mem, item["idle"], item["total"],
               price, item["machine_id"]))


# ------------------------------------------------------------------ 子命令
def cmd_list_regions(cfg, args):
    client = AutoDL(resolve_token(cfg, args.token), cfg["api_base"], cfg["timeout_seconds"])
    regions = client.regions()
    log("AutoDL 当前地区共 %d 个：" % len(regions))
    for reg in regions:
        print("  %-12s  %-14s  datacenter=%s"
              % (reg.get("region_sign", ""), reg.get("region_name", ""),
                 reg.get("data_center", "")))
    return 0


def cmd_list_gpus(cfg, args):
    client = AutoDL(resolve_token(cfg, args.token), cfg["api_base"], cfg["timeout_seconds"])
    regions = client.regions()
    picked = resolve_regions(regions, args.regions or cfg.get("regions"))
    if not picked:
        log("没有可用的地区", "ERROR")
        return 2
    signs = [s for s, _ in picked]
    log("查询地区 GPU 型号: %s" % ", ".join(signs))
    try:
        data = client.region_gpu_types(signs)
    except Exception as exc:
        log("region/gpu_type 查询失败: %s" % exc, "ERROR")
        return 2
    table = parse_region_gpu_types(data)
    if not table:
        log("没有返回型号数据（返回体：%s）" % json.dumps(data, ensure_ascii=False)[:400], "WARN")
        return 1
    for name, info in sorted(table.items()):
        idle = info.get("gpu_idle_num")
        total = info.get("gpu_total_number", info.get("gpu_number"))
        print("  %-22s 空闲 %s / %s" % (name, idle, total))
    return 0


def cmd_check(cfg, args):
    token = resolve_token(cfg, args.token)
    if not token:
        log("没有 token。获取方式见 README，或先跑 --login", "ERROR")
        return 2
    client = AutoDL(token, cfg["api_base"], cfg["timeout_seconds"])
    try:
        regions = client.regions()
        log("token 可用，地区列表返回 %d 条" % len(regions))
    except Exception as exc:
        log("token 校验失败: %s" % exc, "ERROR")
        return 1
    try:
        resp = client.search([], page_index=1, page_size=1)
        rows, _ = extract_list(resp)
        log("机器列表接口可用，返回 %d 条（取样）" % len(rows))
    except Exception as exc:
        log("机器列表接口不可用: %s" % exc, "ERROR")
        return 1
    return 0


def cmd_login(cfg, args):
    if not args.phone:
        log("需要 --phone 和密码（密码通过交互输入，不写命令行明文）", "ERROR")
        return 2
    password = args.password
    if not password:
        import getpass
        password = getpass.getpass("AutoDL 密码: ")
    client = AutoDL(None, cfg["api_base"], cfg["timeout_seconds"])
    try:
        token = client.login(args.phone, password)
    except Exception as exc:
        log("登录失败: %s" % exc, "ERROR")
        log("可改用手动方式：浏览器登录 autodl.com → F12 → Application → Local Storage → "
            "https://www.autodl.com → 复制 token 值", "INFO")
        return 1
    cfg["authorization"] = token
    save_config(cfg)
    log("登录成功，token 已写入 config.json")
    return 0


def cmd_test_email(cfg, args):
    """发一封测试邮件，验证通知通道。"""
    notif = cfg.get("notification") or {}
    email_cfg = notif.get("email") or {}
    resend_key = str(notif.get("resend_api_key") or "").strip()
    body = ("这是一封测试邮件。\n时间：%s\n"
            "如果收到，说明邮件通道已配置完成。" % now_str())
    if resend_key:
        ok, detail = notify_resend(cfg, "AutoDL 监控测试邮件", body)
    else:
        ok, detail = notify_email(cfg, "AutoDL 监控测试邮件", body)
    log("邮件测试：%s %s" % ("成功" if ok else "失败", detail),
        "INFO" if ok else "ERROR")
    if not ok:
        log("当前收件人：%s" % ", ".join(email_cfg.get("to") or []), "INFO")
    return 0 if ok else 1


def cmd_monitor(cfg, args):
    token = resolve_token(cfg, args.token)
    if not token:
        log("没有 token。获取方式见 README，或先跑 --login", "ERROR")
        return 2

    client = AutoDL(token, cfg["api_base"], cfg["timeout_seconds"])
    try:
        all_regions = client.regions()
    except Exception as exc:
        log("拉取地区列表失败: %s" % exc, "ERROR")
        return 2

    picked = resolve_regions(all_regions, args.regions if args.regions else cfg.get("regions"))
    if not picked:
        log("没有匹配到任何地区，请用 --list-regions 查看当前地区名", "ERROR")
        return 2

    wanted_gpus = args.gpus if args.gpus else cfg.get("gpus")
    gpu_match = str(cfg.get("gpu_match") or "exact").lower()
    if gpu_match not in ("exact", "loose"):
        gpu_match = "exact"
    min_idle = args.min_idle if args.min_idle is not None else int(cfg.get("min_idle_gpu") or 1)
    interval = args.interval if args.interval is not None else int(cfg.get("interval_seconds") or 30)
    only_change = bool(cfg.get("notify_on_change_only", True))
    cooldown = int(cfg.get("cooldown_seconds") or 0)
    open_browser = bool(cfg.get("open_browser_on_new", True))
    market_url = cfg.get("market_url") or "https://www.autodl.com/market/list/"

    sign_to_name = {s: (n or s) for s, n in picked}
    log("监控启动")
    log("  地区: %s" % ", ".join("%s(%s)" % (sign_to_name.get(s, s), s) for s, _ in picked))
    log("  GPU : %s" % (", ".join(wanted_gpus) if wanted_gpus else "不限"))
    log("  匹配: %s" % ("完全同名" if gpu_match == "exact" else "包含匹配(含 *2 等变体)"))
    log("  条件: 空闲GPU >= %d, 间隔 %ds, 通知策略=%s"
        % (min_idle, interval, "仅变化时" if only_change else "每次都通知"))

    last_state = None
    last_notify_ts = 0.0
    last_browser_ts = 0.0
    first_round = True

    while True:
        try:
            machines = fetch_machines(client, [s for s, _ in picked], cfg)
        except Exception as exc:
            log("本轮查询失败: %s" % exc, "ERROR")
            if args.once:
                return 1
            time.sleep(min(interval, 10))
            continue

        present = sorted({m["gpu_name"] for m in machines if m["gpu_name"]})
        allowed = resolve_gpu_names(present, wanted_gpus, gpu_match)
        if allowed is not None:
            machines = [m for m in machines if m["gpu_name"] in allowed]

        matched = sorted(
            [m for m in machines if m["idle"] >= min_idle],
            key=lambda m: (-m["idle"], m["price"] if m["price"] is not None else 1e9))

        state = tuple(sorted((m["machine_id"], m["gpu_name"], m["idle"]) for m in matched))
        changed = (state != last_state)

        if matched:
            log("命中 %d 台:" % len(matched))
            for m in matched:
                log("    " + describe(m))
        else:
            log("本轮无匹配（该地区在售 %d 种型号，扫描 %d 台）"
                % (len(present), len(machines)))

        if first_round:
            log("该地区在售型号：%s" % (", ".join(present) if present else "无"))
            log("首轮扫描完成，状态：%s" % ("有货" if matched else "无货"))
            first_round = False

        should_notify = bool(matched) and (not only_change or changed or last_state is None)
        if should_notify:
            now_ts = time.time()
            if now_ts - last_notify_ts >= cooldown:
                title = "AutoDL 有空闲 GPU！"
                lines = ["条件：地区 %s / GPU %s / 空闲>=%d"
                         % ("、".join(sign_to_name.get(s, s) for s, _ in picked),
                            "、".join(wanted_gpus) if wanted_gpus else "不限",
                            min_idle),
                         "时间：%s" % now_str(), ""]
                lines += ["- " + describe(m) for m in matched]
                lines += ["", "去下单：%s" % market_url]
                content = "\n".join(lines)
                log("=== 触发告警 ===")
                log(content)
                beep()
                send_notifications(cfg, title, content)
                last_notify_ts = now_ts
                if open_browser and now_ts - last_browser_ts >= max(cooldown, 60):
                    try:
                        webbrowser.open(market_url)
                        log("已自动打开算力商城页面")
                    except Exception:
                        pass
                    last_browser_ts = now_ts
            else:
                log("状态有变化但处于冷却期(%ds)内，本次不重复推送" % cooldown, "INFO")

        last_state = state

        if args.once:
            return 0
        time.sleep(max(1, interval))


# ------------------------------------------------------------------ 入口
def build_parser():
    p = argparse.ArgumentParser(
        description="AutoDL 空闲 GPU 监控器（指定地区 + 指定 GPU 型号）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    p.add_argument("--token", help="AutoDL token（优先于配置文件）")
    p.add_argument("--regions", nargs="*", help="地区，如 北京A区 内蒙B区 / beijing-A")
    p.add_argument("--gpus", nargs="*", help='GPU 型号，如 "RTX 4090"')
    p.add_argument("--min-idle", dest="min_idle", type=int, help="最少空闲 GPU 数")
    p.add_argument("--interval", type=int, help="轮询间隔秒数")
    p.add_argument("--once", action="store_true", help="只查询一次就退出")
    p.add_argument("--list-regions", action="store_true", help="列出所有地区")
    p.add_argument("--list-gpus", action="store_true", help="列出在售 GPU 型号")
    p.add_argument("--check", action="store_true", help="检查 token 是否有效")
    p.add_argument("--test-email", action="store_true", help="发一封测试邮件验证通道")
    p.add_argument("--login", action="store_true", help="手机号密码登录并写回配置")
    p.add_argument("--phone", help="登录手机号")
    p.add_argument("--password", help="登录密码（不传则交互输入）")
    return p


def main():
    fix_stdout()
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    if args.login:
        return cmd_login(cfg, args)
    if args.list_regions:
        return cmd_list_regions(cfg, args)
    if args.list_gpus:
        return cmd_list_gpus(cfg, args)
    if args.check:
        return cmd_check(cfg, args)
    if args.test_email:
        return cmd_test_email(cfg, args)
    return cmd_monitor(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已手动退出", "INFO")
        sys.exit(0)
