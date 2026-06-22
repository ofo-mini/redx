#!/usr/bin/env python3
import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pymysql
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


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

SOURCE_TABLE = "xhs_url"
RESULT_TABLE = "xhs_feed_api_capture"
DEFAULT_TARGET_PATHS = [
    "/api/sns/web/v1/feed",
    "/api/sns/h5/v1/note_info",
]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def load_db_config():
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


def ensure_result_table(conn):
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{RESULT_TABLE}` (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        `source_id` BIGINT NULL,
        `url` VARCHAR(2048) NOT NULL,
        `url_hash` CHAR(64) NOT NULL,
        `source_note_id` VARCHAR(64) NULL,
        `xsec_token` TEXT NULL,
        `xsec_source` VARCHAR(64) NULL,
        `api_url` TEXT NULL,
        `request_headers` LONGTEXT NULL,
        `request_body` LONGTEXT NULL,
        `response_status` INT NULL,
        `response_headers` LONGTEXT NULL,
        `response_body` LONGTEXT NULL,
        `error_message` TEXT NULL,
        `captured_at` DATETIME NOT NULL,
        `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uniq_url_hash` (`url_hash`),
        KEY `idx_source_id` (`source_id`),
        KEY `idx_captured_at` (`captured_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with conn.cursor() as cursor:
        cursor.execute(ddl)
    conn.commit()


def get_source_columns(conn):
    sql = """
    SELECT COLUMN_NAME, COLUMN_KEY
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
    ORDER BY ORDINAL_POSITION
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (SOURCE_TABLE,))
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"table `{SOURCE_TABLE}` does not exist")
    columns = [row["COLUMN_NAME"] for row in rows]
    if "url" not in columns:
        raise RuntimeError(f"table `{SOURCE_TABLE}` has no `url` column")
    pk_column = None
    for row in rows:
        if row["COLUMN_KEY"] == "PRI":
            pk_column = row["COLUMN_NAME"]
            break
    if not pk_column and "id" in columns:
        pk_column = "id"
    return pk_column


def fetch_source_rows(conn, pk_column, limit, offset):
    select_pk = f"`{pk_column}` AS source_id," if pk_column else "NULL AS source_id,"
    order_by = f"ORDER BY `{pk_column}`" if pk_column else ""
    sql = f"""
    SELECT {select_pk} `url`
    FROM `{SOURCE_TABLE}`
    WHERE `url` IS NOT NULL AND TRIM(`url`) <> ''
    {order_by}
    LIMIT %s OFFSET %s
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (limit, offset))
        return cursor.fetchall()


def parse_note_meta(url):
    parsed = urlparse(url)
    source_note_id = parsed.path.rstrip("/").split("/")[-1] or None
    query = parse_qs(parsed.query)
    xsec_token = (query.get("xsec_token") or [None])[0]
    xsec_source = (query.get("xsec_source") or [None])[0]
    return source_note_id, xsec_token, xsec_source


def to_json(data):
    return json.dumps(data, ensure_ascii=False) if data is not None else None


def upsert_result(conn, source_id, url, payload):
    sql = f"""
    INSERT INTO `{RESULT_TABLE}` (
        `source_id`,
        `url`,
        `url_hash`,
        `source_note_id`,
        `xsec_token`,
        `xsec_source`,
        `api_url`,
        `request_headers`,
        `request_body`,
        `response_status`,
        `response_headers`,
        `response_body`,
        `error_message`,
        `captured_at`
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        `source_id` = VALUES(`source_id`),
        `source_note_id` = VALUES(`source_note_id`),
        `xsec_token` = VALUES(`xsec_token`),
        `xsec_source` = VALUES(`xsec_source`),
        `api_url` = VALUES(`api_url`),
        `request_headers` = VALUES(`request_headers`),
        `request_body` = VALUES(`request_body`),
        `response_status` = VALUES(`response_status`),
        `response_headers` = VALUES(`response_headers`),
        `response_body` = VALUES(`response_body`),
        `error_message` = VALUES(`error_message`),
        `captured_at` = VALUES(`captured_at`)
    """
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            (
                source_id,
                url,
                url_hash,
                payload.get("source_note_id"),
                payload.get("xsec_token"),
                payload.get("xsec_source"),
                payload.get("api_url"),
                payload.get("request_headers"),
                payload.get("request_body"),
                payload.get("response_status"),
                payload.get("response_headers"),
                payload.get("response_body"),
                payload.get("error_message"),
                captured_at,
            ),
        )


def load_cookie_header():
    raw_cookie = os.getenv("XHS_COOKIE", "").strip()
    cookies = []
    if not raw_cookie:
        return cookies
    for chunk in raw_cookie.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.strip().split("=", 1)
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".xiaohongshu.com",
                "path": "/",
            }
        )
    return cookies


def capture_feed_response(context, url, timeout_ms, target_paths):
    result = {
        "api_url": None,
        "request_headers": None,
        "request_body": None,
        "response_status": None,
        "response_headers": None,
        "response_body": None,
        "error_message": None,
    }
    page = context.new_page()
    feed_response = None

    def on_response(response):
        nonlocal feed_response
        if any(path in response.url for path in target_paths) and feed_response is None:
            feed_response = response

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        deadline = page.wait_for_timeout
        elapsed = 0
        step = 500
        while feed_response is None and elapsed < timeout_ms:
            deadline(step)
            elapsed += step
        if feed_response is None:
            result["error_message"] = "feed api not captured before timeout"
            return result

        request = feed_response.request
        result["api_url"] = feed_response.url
        result["request_headers"] = to_json(request.headers)
        result["request_body"] = request.post_data
        result["response_status"] = feed_response.status
        result["response_headers"] = to_json(feed_response.headers)
        result["response_body"] = feed_response.text()
        return result
    except PlaywrightTimeoutError:
        result["error_message"] = "page navigation timeout"
        return result
    except Exception as exc:
        result["error_message"] = str(exc)
        return result
    finally:
        page.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Capture XHS feed API responses with a browser.")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--target-path",
        action="append",
        dest="target_paths",
        default=None,
        help="API path fragment to capture. Can be provided multiple times.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    conn = pymysql.connect(**load_db_config())
    try:
        ensure_result_table(conn)
        pk_column = get_source_columns(conn)
        cookies = load_cookie_header()
        target_paths = args.target_paths or DEFAULT_TARGET_PATHS

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
            )
            if cookies:
                context.add_cookies(cookies)

            total_processed = 0
            offset = args.offset
            hard_limit = args.limit

            while True:
                batch_size = args.batch_size
                if hard_limit:
                    remaining = hard_limit - total_processed
                    if remaining <= 0:
                        break
                    batch_size = min(batch_size, remaining)
                rows = fetch_source_rows(conn, pk_column, batch_size, offset)
                if not rows:
                    break

                for row in rows:
                    source_id = row["source_id"]
                    url = row["url"].strip()
                    source_note_id, xsec_token, xsec_source = parse_note_meta(url)
                    payload = capture_feed_response(context, url, args.timeout_ms, target_paths)
                    payload["source_note_id"] = source_note_id
                    payload["xsec_token"] = xsec_token
                    payload["xsec_source"] = xsec_source
                    upsert_result(conn, source_id, url, payload)
                    conn.commit()
                    total_processed += 1
                    logging.info(
                        "processed=%s source_id=%s status=%s error=%s url=%s",
                        total_processed,
                        source_id,
                        payload["response_status"],
                        payload["error_message"],
                        url,
                    )
                offset += len(rows)
            browser.close()
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
