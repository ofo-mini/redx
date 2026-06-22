#!/usr/bin/env python3
import hashlib
import json
import logging
import os
import threading
from datetime import datetime

import pymysql
import requests as http_requests
from cachetools import TTLCache
from dbutils.pooled_db import PooledDB
from flask import Flask, jsonify, request

log = logging.getLogger(__name__)

app = Flask(__name__)

API_TOKEN = os.getenv("API_TOKEN", "NkruZETCBN4dzeMUz")


@app.before_request
def check_token():
    if request.path == "/health":
        return
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.args.get("token", "")
    if token != API_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

DEFAULT_DB_CONFIG = {
    "host": "qq.rwlb.rds.aliyuncs.com",
    "user": "data",
    "password": "AbHGL8jMwMPmzM",
    "database": "data",
    "port": 3306,
    "connect_timeout": 5,
    "read_timeout": 10,
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": True,
}

# 缓存：最多10000条，TTL 5分钟
_cache = TTLCache(maxsize=10000, ttl=300)
_cache_lock = threading.Lock()

# 负缓存：兜底也失败的 note_id，短 TTL 60秒，避免反复调外部 API
_neg_cache = TTLCache(maxsize=5000, ttl=60)
_neg_cache_lock = threading.Lock()


def _build_db_config():
    config = DEFAULT_DB_CONFIG.copy()
    env_map = {
        "host": "DB_HOST",
        "user": "DB_USER",
        "password": "DB_PASSWORD",
        "database": "DB_NAME",
        "port": "DB_PORT",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value:
            config[key] = int(value) if key == "port" else value
    return config


def _create_pool():
    return PooledDB(
        creator=pymysql,
        maxconnections=10,
        mincached=4,
        maxcached=10,
        blocking=True,
        maxusage=2000,
        ping=1,
        **_build_db_config(),
    )


_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = _create_pool()
    return _pool


COLUMNS = """c.id, c.source_id, c.url, c.http_status, c.final_url,
    c.title, c.description, c.keywords,
    c.initial_state_json, c.parsed_summary, c.parse_error,
    c.captured_at, c.created_at, c.updated_at"""

JSON_FIELDS = frozenset(("initial_state_json", "parsed_summary"))

INVALID_TITLE = "小红书 - 你访问的页面不见了"

FALLBACK_API_URL = "https://api.302.ai/tools/xiaohongshu/app/get_note_info"
FALLBACK_API_KEY = os.getenv("FALLBACK_API_KEY", "sk-tmDyrGJQD3aToZICppt9Dik6tXrBA7Sd71xaDwxc6aE7Ysqt")

# ── 兜底 API ──

def fetch_from_fallback(note_id):
    try:
        resp = http_requests.get(
            FALLBACK_API_URL,
            params={"note_id": note_id},
            headers={"Authorization": f"Bearer {FALLBACK_API_KEY}"},
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        # 提取 note 数据: data.data[0].note_list[0]
        inner = body.get("data", {})
        if not isinstance(inner, dict) or inner.get("code") != 0:
            return None
        items = inner.get("data", [])
        if not items:
            return None
        note_list = items[0].get("note_list", [])
        if not note_list:
            return None
        return note_list[0]
    except Exception as e:
        log.warning("fallback API failed for %s: %s", note_id, e)
        return None


# ── 回写数据库 ──

def _ensure_xhs_url(conn, note_id):
    """确保 xhs_url 中有该 note_id，返回其 id"""
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM xhs_url WHERE note_id = %s", (note_id,))
        row = cursor.fetchone()
        if row:
            return row["id"]
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        cursor.execute(
            "INSERT INTO xhs_url (note_id, url) VALUES (%s, %s)",
            (note_id, url),
        )
        return cursor.lastrowid


def save_fallback_to_db(note_id, note_data, api_response):
    """将兜底 API 的结果写入 xhs_initial_state_capture"""
    try:
        conn = get_pool().connection()
        try:
            source_id = _ensure_xhs_url(conn, note_id)
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
            url_hash = hashlib.sha256(url.encode()).hexdigest()
            title = note_data.get("title", "")
            desc = note_data.get("desc", "")
            initial_state_json = json.dumps(api_response, ensure_ascii=False)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO xhs_initial_state_capture (
                        source_id, url, url_hash, http_status, final_url,
                        title, description, initial_state_json,
                        parsed_summary, captured_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        http_status = VALUES(http_status),
                        final_url = VALUES(final_url),
                        title = VALUES(title),
                        description = VALUES(description),
                        initial_state_json = VALUES(initial_state_json),
                        parsed_summary = VALUES(parsed_summary),
                        captured_at = VALUES(captured_at)
                    """,
                    (
                        source_id, url, url_hash, 200, url,
                        title, desc, initial_state_json,
                        json.dumps({"source": "fallback_api"}, ensure_ascii=False),
                        now,
                    ),
                )
            log.info("saved fallback result to DB for note_id=%s source_id=%s", note_id, source_id)
        finally:
            conn.close()
    except Exception as e:
        log.warning("failed to save fallback to DB for %s: %s", note_id, e)


# ── 序列化 & 查询 ──

def serialize_row(row):
    if row is None:
        return None
    result = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, str) and key in JSON_FIELDS:
            try:
                result[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[key] = value
        else:
            result[key] = value
    return result


def _is_valid_db_result(result):
    if result is None:
        return False
    title = result.get("title") or ""
    return INVALID_TITLE not in title


def query_note_db(note_id):
    sql = f"""
        SELECT {COLUMNS}
        FROM xhs_url u
        JOIN xhs_initial_state_capture c ON c.source_id = u.id
        WHERE u.note_id = %s
        ORDER BY c.captured_at DESC
        LIMIT 1
    """
    conn = get_pool().connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (note_id,))
            row = cursor.fetchone()
    finally:
        conn.close()
    return serialize_row(row)


def query_note(note_id):
    cache_key = note_id
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    result = query_note_db(note_id)

    # 兜底：数据库没查到或页面不存在，走外部 API
    if not _is_valid_db_result(result):
        # 检查负缓存，避免短时间内重复调外部 API
        with _neg_cache_lock:
            if note_id in _neg_cache:
                # 兜底最近也失败了，直接返回 DB 原始结果
                if result is not None:
                    with _cache_lock:
                        _cache[cache_key] = result
                return result

        note_data = fetch_from_fallback(note_id)
        if note_data is not None:
            # 异步回写数据库
            threading.Thread(
                target=save_fallback_to_db,
                args=(note_id, note_data, note_data),
                daemon=True,
            ).start()
            # 构造返回结果
            result = {
                "source": "fallback_api",
                "note_id": note_id,
                "title": note_data.get("title", ""),
                "desc": note_data.get("desc", ""),
                "type": note_data.get("type", ""),
                "liked_count": note_data.get("liked_count", 0),
                "collected_count": note_data.get("collected_count", 0),
                "comments_count": note_data.get("comments_count", 0),
                "shared_count": note_data.get("shared_count", 0),
                "ip_location": note_data.get("ip_location", ""),
                "time": note_data.get("time", 0),
                "images_list": note_data.get("images_list", []),
                "user": note_data.get("user", {}),
                "data": note_data,
            }
        else:
            # 兜底也失败了，记入负缓存
            with _neg_cache_lock:
                _neg_cache[note_id] = True

    if result is not None:
        with _cache_lock:
            _cache[cache_key] = result
    return result


@app.route("/api/note/<note_id>", methods=["GET"])
def get_note(note_id):
    result = query_note(note_id)
    if result is None:
        return jsonify({"error": "not found", "note_id": note_id}), 404
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
