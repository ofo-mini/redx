#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote_plus, unquote_plus

import pymysql
from pymongo import MongoClient
from pymongo.errors import BulkWriteError
from curl_cffi import requests
from xhs_queue_common import load_env_file


DEFAULT_DB_CONFIG = {
    "host": "qq.rwlb.rds.aliyuncs.com",
    "user": "data",
    "password": "AbHGL8jMwMPmzM",
    "database": "data",
    "port": 3306,
    "connect_timeout": 5,
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False,
}

RESULT_TABLE = "xhs_initial_state_capture"
DEFAULT_MONGODB_DB = "data"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_IMPERSONATE = "chrome124"
DEFAULT_PROXY_API_URL = (
    "http://api.xiequ.cn/VAD/GetIp.aspx?act=get&uid=177346"
    "&vkey=A3ED2598E7897DEFBA39A28839E3178B&num=200&time=30"
    "&plat=1&re=0&type=1&so=1&ow=1&spl=1&addr=&db=1"
)
DEFAULT_PROXY_API_KEY = ""
INSERT_SQL = f"""
INSERT INTO `{RESULT_TABLE}` (
    `source_id`,
    `note_id`,
    `url`,
    `url_hash`,
    `http_status`,
    `final_url`,
    `title`,
    `description`,
    `keywords`,
    `proxy_url`,
    `proxy_out_ip`,
    `proxy_fetch_id`,
    `proxy_payload`,
    `retry_count`,
    `initial_state_json`,
    `parsed_summary`,
    `parse_error`,
    `captured_at`
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def load_db_config():
    load_env_file()
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


def load_mongodb_uri():
    load_env_file()
    uri = os.getenv("MONGODB_HOST", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_HOST is not set")
    if "://" not in uri:
        uri = f"mongodb://{uri}"
    scheme, remainder = uri.split("://", 1)
    authority, sep, tail = remainder.partition("/")
    if "@" in authority:
        userinfo, hosts = authority.rsplit("@", 1)
        if ":" in userinfo:
            username, password = userinfo.split(":", 1)
            userinfo = f"{quote_plus(unquote_plus(username))}:{quote_plus(unquote_plus(password))}"
        else:
            userinfo = quote_plus(unquote_plus(userinfo))
        authority = f"{userinfo}@{hosts}"
    uri = f"{scheme}://{authority}"
    if sep:
        uri = f"{uri}/{tail}"
    return uri


def load_mongodb_database_name():
    load_env_file()
    return os.getenv("MONGODB_DB", os.getenv("DB_NAME", DEFAULT_MONGODB_DB))


def load_mongodb_collection_name():
    load_env_file()
    return os.getenv("MONGODB_COLLECTION", RESULT_TABLE)


def open_mongo_collection():
    client = MongoClient(load_mongodb_uri())
    database = client[load_mongodb_database_name()]
    collection = database[load_mongodb_collection_name()]
    return client, collection


def ensure_mongo_indexes(collection):
    collection.create_index("source_id")
    collection.create_index("note_id")
    collection.create_index("captured_at")


def ensure_result_table(conn):
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{RESULT_TABLE}` (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `source_id` BIGINT NULL,
        `note_id` VARCHAR(64) NULL,
        `url` VARCHAR(2048) NOT NULL,
        `url_hash` CHAR(64) NOT NULL,
        `http_status` INT NULL,
        `final_url` TEXT NULL,
        `title` TEXT NULL,
        `description` LONGTEXT NULL,
        `keywords` LONGTEXT NULL,
        `proxy_url` TEXT NULL,
        `proxy_out_ip` VARCHAR(64) NULL,
        `proxy_fetch_id` VARCHAR(128) NULL,
        `proxy_payload` LONGTEXT NULL,
        `retry_count` INT NOT NULL DEFAULT 0,
        `initial_state_raw` LONGTEXT NULL,
        `initial_state_json` LONGTEXT NULL,
        `parsed_summary` LONGTEXT NULL,
        `parse_error` TEXT NULL,
        `captured_at` DATETIME NOT NULL,
        `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        KEY `idx_source_id` (`source_id`),
        KEY `idx_url_hash` (`url_hash`),
        KEY `idx_note_id` (`note_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with conn.cursor() as cursor:
        cursor.execute(ddl)
    conn.commit()
    extra_columns = {
        "source_id": "ALTER TABLE `{}` ADD COLUMN `source_id` BIGINT NULL AFTER `id`".format(RESULT_TABLE),
        "note_id": "ALTER TABLE `{}` ADD COLUMN `note_id` VARCHAR(64) NULL AFTER `source_id`".format(RESULT_TABLE),
        "proxy_url": "ALTER TABLE `{}` ADD COLUMN `proxy_url` TEXT NULL AFTER `keywords`".format(RESULT_TABLE),
        "proxy_out_ip": "ALTER TABLE `{}` ADD COLUMN `proxy_out_ip` VARCHAR(64) NULL AFTER `proxy_url`".format(RESULT_TABLE),
        "proxy_fetch_id": "ALTER TABLE `{}` ADD COLUMN `proxy_fetch_id` VARCHAR(128) NULL AFTER `proxy_out_ip`".format(RESULT_TABLE),
        "proxy_payload": "ALTER TABLE `{}` ADD COLUMN `proxy_payload` LONGTEXT NULL AFTER `proxy_fetch_id`".format(RESULT_TABLE),
        "retry_count": "ALTER TABLE `{}` ADD COLUMN `retry_count` INT NOT NULL DEFAULT 0 AFTER `proxy_payload`".format(RESULT_TABLE),
    }
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (RESULT_TABLE,),
        )
        existing = {row["COLUMN_NAME"] for row in cursor.fetchall()}
        for column_name, alter_sql in extra_columns.items():
            if column_name not in existing:
                cursor.execute(alter_sql)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = 'idx_source_id'
            """,
            (RESULT_TABLE,),
        )
        if cursor.fetchone()["COUNT(*)"] == 0:
            cursor.execute(f"ALTER TABLE `{RESULT_TABLE}` ADD INDEX `idx_source_id` (`source_id`)")
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = 'idx_url_hash'
            """,
            (RESULT_TABLE,),
        )
        if cursor.fetchone()["COUNT(*)"] == 0:
            cursor.execute(f"ALTER TABLE `{RESULT_TABLE}` ADD INDEX `idx_url_hash` (`url_hash`)")
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = 'idx_note_id'
            """,
            (RESULT_TABLE,),
        )
        if cursor.fetchone()["COUNT(*)"] == 0:
            cursor.execute(f"ALTER TABLE `{RESULT_TABLE}` ADD INDEX `idx_note_id` (`note_id`)")
    conn.commit()


def extract_meta(html, name):
    patterns = {
        "title": r"<title>(.*?)</title>",
        "description": r'<meta name="description" content="(.*?)">',
        "keywords": r'<meta name="keywords" content="(.*?)">',
    }
    match = re.search(patterns[name], html, re.S)
    return match.group(1).strip() if match else None


def extract_initial_state_raw(html):
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    brace = 0
    in_str = False
    escape = False
    end = None

    for idx, ch in enumerate(html[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                end = idx + 1
                break
    return html[start:end] if end else None


def normalize_js_object_to_json(raw):
    chars = []
    i = 0
    in_str = False
    escape = False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            chars.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            chars.append(ch)
            i += 1
            continue

        if raw.startswith("undefined", i):
            chars.append("null")
            i += len("undefined")
            continue

        chars.append(ch)
        i += 1
    return "".join(chars)


def summarize_state(state):
    summary = {"top_keys": list(state.keys())[:20], "matched_paths": []}

    def walk(obj, path="root"):
        if len(summary["matched_paths"]) >= 50:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                next_path = f"{path}.{key}"
                if isinstance(value, str):
                    if any(token in value for token in ("生活无解", "户外撒野", "百褶裙", "69b9487a000000002202929f")):
                        summary["matched_paths"].append(
                            {"path": next_path, "value": value[:500]}
                        )
                walk(value, next_path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:100]):
                walk(item, f"{path}[{idx}]")

    walk(state)
    return summary


def extract_last_update_time(value, note_id=None):
    if isinstance(value, dict) and note_id:
        try:
            note_detail = (((value.get("note") or {}).get("noteDetailMap") or {}).get(str(note_id)) or {}).get("note") or {}
            if note_detail.get("lastUpdateTime") is not None:
                return note_detail.get("lastUpdateTime")
        except Exception:
            pass
    if isinstance(value, dict):
        if "lastUpdateTime" in value:
            return value.get("lastUpdateTime")
        for child in value.values():
            found = extract_last_update_time(child, note_id=note_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = extract_last_update_time(child, note_id=note_id)
            if found is not None:
                return found
    return None


def build_upsert_params(payload):
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url_hash = hashlib.sha256(payload["url"].encode("utf-8")).hexdigest()
    initial_state_json = payload.get("initial_state_json")
    if isinstance(initial_state_json, (dict, list)):
        initial_state_json = json.dumps(initial_state_json, ensure_ascii=False)
    return (
        payload.get("source_id"),
        payload.get("note_id"),
        payload["url"],
        url_hash,
        payload["http_status"],
        payload["final_url"],
        payload["title"],
        payload["description"],
        payload["keywords"],
        payload["proxy_url"],
        payload["proxy_out_ip"],
        payload["proxy_fetch_id"],
        payload["proxy_payload"],
        payload.get("retry_count", 0),
        initial_state_json,
        payload["parsed_summary"],
        payload["parse_error"],
        captured_at,
    )


def build_insert_document(payload):
    captured_at = datetime.now()
    initial_state_json = payload.get("initial_state_json")
    return {
        "source_id": payload.get("source_id"),
        "note_id": payload.get("note_id"),
        "url": payload["url"],
        "http_status": payload["http_status"],
        "final_url": payload["final_url"],
        "title": payload["title"],
        "description": payload["description"],
        "keywords": payload["keywords"],
        "proxy_url": payload["proxy_url"],
        "proxy_out_ip": payload["proxy_out_ip"],
        "proxy_fetch_id": payload["proxy_fetch_id"],
        "retry_count": payload.get("retry_count", 0),
        "initial_state_json": initial_state_json,
        "lastUpdateTime": extract_last_update_time(initial_state_json, note_id=payload.get("note_id")),
        "parsed_summary": payload["parsed_summary"],
        "parse_error": payload["parse_error"],
        "captured_at": captured_at,
    }


def extract_note_id_from_url(url):
    match = re.search(r"/explore/([^/?]+)", url or "")
    return match.group(1) if match else None


def upsert_result(conn, payload):
    with conn.cursor() as cursor:
        cursor.execute(INSERT_SQL, build_upsert_params(payload))


def upsert_results(conn, payloads):
    params = [build_upsert_params(payload) for payload in payloads]
    with conn.cursor() as cursor:
        cursor.executemany(INSERT_SQL, params)


def insert_result_mongo(collection, payload):
    collection.insert_one(build_insert_document(payload))


def insert_results_mongo(collection, payloads):
    documents = [build_insert_document(payload) for payload in payloads]
    if not documents:
        return
    collection.insert_many(documents, ordered=False)


def get_result_state_map(collection, source_ids, retryable_regex):
    ids = [source_id for source_id in source_ids if source_id is not None]
    if not ids:
        return {}
    state_map = {}
    cursor = collection.find(
        {"source_id": {"$in": ids}},
        {"source_id": 1, "retry_count": 1, "parse_error": 1},
        no_cursor_timeout=False,
    )
    for row in cursor:
        source_id = row.get("source_id")
        if source_id is None:
            continue
        state = state_map.setdefault(
            source_id,
            {"has_success": False, "max_retry_count": 0, "has_retryable_error": False},
        )
        retry_count = int(row.get("retry_count", 0) or 0)
        if retry_count > state["max_retry_count"]:
            state["max_retry_count"] = retry_count
        parse_error = row.get("parse_error")
        if parse_error is None:
            state["has_success"] = True
        elif retryable_regex.search(parse_error):
            state["has_retryable_error"] = True
    return state_map


def parse_args():
    parser = argparse.ArgumentParser(description="Capture and parse XHS window.__INITIAL_STATE__.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--source-id", type=int, default=None)
    parser.add_argument("--cookie", default=os.getenv("XHS_COOKIE", ""))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument("--proxy-api-url", default=os.getenv("PROXY_API_URL", DEFAULT_PROXY_API_URL))
    parser.add_argument("--proxy-api-key", default=os.getenv("PROXY_API_KEY", DEFAULT_PROXY_API_KEY))
    return parser.parse_args()


def normalize_proxy_url(proxy_url):
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    if proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://"):]
    return proxy_url


def fetch_proxy(args):
    headers = {"X-API-KEY": args.proxy_api_key} if getattr(args, "proxy_api_key", "") else None
    response = requests.get(args.proxy_api_url, headers=headers, timeout=args.timeout)
    body = response.text.strip()
    if not body:
        raise RuntimeError(f"proxy api returned empty body: status={response.status_code}")
    if body.startswith("{") or body.startswith("["):
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"proxy api failed: {payload}")
        data = payload.get("data") or []
        if not data:
            raise RuntimeError(f"proxy api returned empty data: {payload}")
        proxy_info = data[0]
        raw_proxy = proxy_info["pproxy_url"]
        proxy_payload = json.dumps(payload, ensure_ascii=False)
        proxy_out_ip = proxy_info.get("out_ip")
        proxy_fetch_id = proxy_info.get("fetch_id")
    else:
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if ":" not in first_line:
            raise RuntimeError(f"proxy api returned invalid body: {body[:200]}")
        raw_proxy = first_line
        proxy_payload = body
        proxy_out_ip = first_line.split(":", 1)[0]
        proxy_fetch_id = None
    proxy_url = normalize_proxy_url(raw_proxy)
    return {
        "proxy_url": proxy_url,
        "proxy_out_ip": proxy_out_ip,
        "proxy_fetch_id": proxy_fetch_id,
        "proxy_payload": proxy_payload,
    }


def main():
    args = parse_args()
    headers = {
        "user-agent": DEFAULT_USER_AGENT,
        "accept-language": "zh-CN,zh;q=0.9",
    }
    if args.cookie:
        headers["cookie"] = args.cookie

    proxy_info = {
        "proxy_url": None,
        "proxy_out_ip": None,
        "proxy_fetch_id": None,
        "proxy_payload": None,
    }
    request_kwargs = {}
    proxy_info = fetch_proxy(args)
    request_kwargs["proxies"] = {
        "http": proxy_info["proxy_url"],
        "https": proxy_info["proxy_url"],
    }

    response = requests.get(
        args.url,
        headers=headers,
        timeout=args.timeout,
        impersonate=args.impersonate,
        **request_kwargs,
    )
    html = response.text
    initial_state_raw = extract_initial_state_raw(html)
    initial_state_json = None
    parsed_summary = None
    parse_error = None

    if initial_state_raw:
        try:
            normalized_initial_state = normalize_js_object_to_json(initial_state_raw)
            state = json.loads(normalized_initial_state)
            initial_state_json = json.dumps(state, ensure_ascii=False)
            parsed_summary = json.dumps(summarize_state(state), ensure_ascii=False)
        except Exception as exc:
            parse_error = str(exc)
    else:
        parse_error = "window.__INITIAL_STATE__ not found"

    payload = {
        "source_id": args.source_id,
        "url": args.url,
        "http_status": response.status_code,
        "final_url": response.url,
        "title": extract_meta(html, "title"),
        "description": extract_meta(html, "description"),
        "keywords": extract_meta(html, "keywords"),
        "proxy_url": proxy_info["proxy_url"],
        "proxy_out_ip": proxy_info["proxy_out_ip"],
        "proxy_fetch_id": proxy_info["proxy_fetch_id"],
        "proxy_payload": proxy_info["proxy_payload"],
        "initial_state_raw": initial_state_raw,
        "initial_state_json": initial_state_json,
        "parsed_summary": parsed_summary,
        "parse_error": parse_error,
    }

    conn = pymysql.connect(**load_db_config())
    try:
        ensure_result_table(conn)
        upsert_result(conn, payload)
        conn.commit()
    finally:
        conn.close()

    print(json.dumps(
        {
            "http_status": payload["http_status"],
            "final_url": payload["final_url"],
            "title": payload["title"],
            "description": payload["description"],
            "keywords": payload["keywords"],
            "proxy_url": payload["proxy_url"],
            "proxy_out_ip": payload["proxy_out_ip"],
            "proxy_fetch_id": payload["proxy_fetch_id"],
            "initial_state_raw_len": len(initial_state_raw) if initial_state_raw else 0,
            "parse_error": parse_error,
            "parsed_summary": json.loads(parsed_summary) if parsed_summary else None,
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
