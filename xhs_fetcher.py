#!/usr/bin/env python3
import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import pymysql


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
RESULT_TABLE = "xhs_url_fetch_result"
DEFAULT_BATCH_SIZE = 100
DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
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
        `http_status` INT NULL,
        `final_url` TEXT NULL,
        `response_headers` LONGTEXT NULL,
        `response_body` LONGTEXT NULL,
        `error_message` TEXT NULL,
        `fetched_at` DATETIME NOT NULL,
        `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uniq_url_hash` (`url_hash`),
        KEY `idx_source_id` (`source_id`),
        KEY `idx_fetched_at` (`fetched_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with conn.cursor() as cursor:
        cursor.execute(ddl)
    conn.commit()


def get_source_columns(conn):
    sql = """
    SELECT COLUMN_NAME, DATA_TYPE, COLUMN_KEY
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
    return pk_column, columns


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


def fetch_url(url, timeout, user_agent):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return {
                "http_status": response.getcode(),
                "final_url": response.geturl(),
                "response_headers": json.dumps(dict(response.headers.items()), ensure_ascii=False),
                "response_body": body.decode("utf-8", errors="replace"),
                "error_message": None,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return {
            "http_status": exc.code,
            "final_url": exc.geturl(),
            "response_headers": json.dumps(dict(exc.headers.items()), ensure_ascii=False),
            "response_body": body.decode("utf-8", errors="replace"),
            "error_message": str(exc),
        }
    except Exception as exc:
        return {
            "http_status": None,
            "final_url": None,
            "response_headers": None,
            "response_body": None,
            "error_message": str(exc),
        }


def upsert_result(conn, source_id, url, result):
    sql = f"""
    INSERT INTO `{RESULT_TABLE}` (
        `source_id`,
        `url`,
        `url_hash`,
        `http_status`,
        `final_url`,
        `response_headers`,
        `response_body`,
        `error_message`,
        `fetched_at`
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        `source_id` = VALUES(`source_id`),
        `http_status` = VALUES(`http_status`),
        `final_url` = VALUES(`final_url`),
        `response_headers` = VALUES(`response_headers`),
        `response_body` = VALUES(`response_body`),
        `error_message` = VALUES(`error_message`),
        `fetched_at` = VALUES(`fetched_at`)
    """
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            (
                source_id,
                url,
                url_hash,
                result["http_status"],
                result["final_url"],
                result["response_headers"],
                result["response_body"],
                result["error_message"],
                fetched_at,
            ),
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch URLs from xhs_url and save responses.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 means fetch all rows")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sleep", type=float, default=0.0, help="sleep between requests in seconds")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    db_config = load_db_config()
    conn = pymysql.connect(**db_config)
    try:
        ensure_result_table(conn)
        pk_column, _ = get_source_columns(conn)
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
                result = fetch_url(url, args.timeout, args.user_agent)
                upsert_result(conn, source_id, url, result)
                conn.commit()
                total_processed += 1
                logging.info(
                    "processed=%s source_id=%s status=%s url=%s",
                    total_processed,
                    source_id,
                    result["http_status"],
                    url,
                )
                if args.sleep > 0:
                    time.sleep(args.sleep)
            offset += len(rows)

        logging.info("completed total_processed=%s", total_processed)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
