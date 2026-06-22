#!/usr/bin/env python3
import argparse
import concurrent.futures
import collections
import json
import os
import sys
import threading
import time

import pymysql
import redis

from xhs_initial_state_capture import (
    DEFAULT_IMPERSONATE,
    DEFAULT_USER_AGENT,
    ensure_result_table,
    extract_initial_state_raw,
    extract_meta,
    fetch_proxy,
    load_db_config,
    normalize_js_object_to_json,
    summarize_state,
    upsert_result,
    upsert_results,
)
from curl_cffi import requests


SOURCE_TABLE = "xhs_url"
DEFAULT_BATCH_MULTIPLIER = 10
DEFAULT_PROXY_MAX_USES = 10
DEFAULT_QUEUE_KEY = "xhs:initial_state:pending"
DEFAULT_PROCESSING_KEY = "xhs:initial_state:processing"
DEFAULT_DEDUP_KEY = "xhs:initial_state:queued"
DEFAULT_SEED_BATCH_SIZE = 5000
DEFAULT_DB_BATCH_SIZE = 50
RETRYABLE_ERROR_SQL_REGEX = (
    "curl: \\((7|28|35|52|56)\\)|"
    "SSL_ERROR_SYSCALL|"
    "Connection timed out|"
    "Operation timed out|"
    "Proxy CONNECT aborted|"
    "Could not connect"
)
THREAD_PROXY_STATE = threading.local()


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


def load_redis_config():
    load_env_file()
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        return {"from_url": redis_url}
    config = {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "db": int(os.getenv("REDIS_DB", "0")),
        "decode_responses": True,
        "socket_timeout": float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
        "socket_connect_timeout": float(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
    }
    password = os.getenv("REDIS_PASSWORD")
    if password:
        config["password"] = password
    username = os.getenv("REDIS_USERNAME")
    if username:
        config["username"] = username
    return config


def create_redis_client():
    config = load_redis_config()
    if "from_url" in config:
        return redis.Redis.from_url(
            config["from_url"],
            decode_responses=True,
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
        )
    return redis.Redis(**config)


def get_source_pk_column(conn):
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
    for row in rows:
        if row["COLUMN_KEY"] == "PRI":
            return row["COLUMN_NAME"]
    return "id" if "id" in columns else None


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


def fetch_pending_rows(conn, pk_column, limit, max_retries, after_source_id=None):
    select_pk = f"s.`{pk_column}` AS source_id," if pk_column else "NULL AS source_id,"
    order_by = f"ORDER BY s.`{pk_column}`" if pk_column else ""
    after_clause = ""
    params = [RETRYABLE_ERROR_SQL_REGEX, max_retries]
    if pk_column and after_source_id is not None:
        after_clause = f"AND s.`{pk_column}` > %s"
        params.append(after_source_id)
    sql = f"""
    SELECT {select_pk} s.`url`, COALESCE(r.`retry_count`, 0) AS retry_count
    FROM `{SOURCE_TABLE}` s
    LEFT JOIN `xhs_initial_state_capture` r
      ON r.`source_id` = s.`{pk_column}`
    WHERE s.`url` IS NOT NULL
      AND TRIM(s.`url`) <> ''
      AND (
        r.`id` IS NULL
        OR (
          r.`parse_error` IS NOT NULL
          AND r.`parse_error` REGEXP %s
          AND COALESCE(r.`retry_count`, 0) < %s
        )
      )
      {after_clause}
    {order_by}
    LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def get_queue_member(task):
    source_id = task.get("source_id")
    return str(source_id) if source_id is not None else task["url"]


def recover_processing_tasks(redis_client, pending_key, processing_key):
    recovered = 0
    while redis_client.llen(processing_key) > 0:
        moved = redis_client.rpoplpush(processing_key, pending_key)
        if moved is None:
            break
        recovered += 1
    return recovered


def seed_pending_tasks(redis_client, conn, pk_column, max_retries, limit, seed_batch_size, pending_key, dedup_key):
    seeded = 0
    scanned = 0
    last_source_id = None
    remaining = limit if limit and limit > 0 else None
    while True:
        fetch_limit = seed_batch_size
        if remaining is not None:
            fetch_limit = min(fetch_limit, remaining)
            if fetch_limit <= 0:
                break
        rows = fetch_pending_rows(conn, pk_column, fetch_limit, max_retries, after_source_id=last_source_id)
        if not rows:
            break
        new_tasks = []
        pipe = redis_client.pipeline(transaction=False)
        for row in rows:
            task = {
                "source_id": row["source_id"],
                "url": row["url"].strip(),
                "retry_count": int(row.get("retry_count", 0) or 0),
            }
            member = get_queue_member(task)
            pipe.sadd(dedup_key, member)
        results = pipe.execute()
        for index, row in enumerate(rows):
            if results[index] == 1:
                task = {
                    "source_id": row["source_id"],
                    "url": row["url"].strip(),
                    "retry_count": int(row.get("retry_count", 0) or 0),
                }
                new_tasks.append(json.dumps(task, ensure_ascii=False))
                seeded += 1
        if new_tasks:
            redis_client.rpush(pending_key, *new_tasks)
        scanned += len(rows)
        print(
            json.dumps(
                {
                    "event": "seed_progress",
                    "seeded": seeded,
                    "scanned": scanned,
                    "pending_queue_len": redis_client.llen(pending_key),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if pk_column:
            last_source_id = rows[-1]["source_id"]
        if remaining is not None:
            remaining -= len(rows)
    return {"seeded": seeded, "scanned": scanned}


def pop_redis_tasks(redis_client, pending_key, processing_key, limit):
    tasks = []
    for _ in range(limit):
        payload = redis_client.rpoplpush(pending_key, processing_key)
        if payload is None:
            break
        task = json.loads(payload)
        task["_redis_payload"] = payload
        tasks.append(task)
    return tasks


def ack_redis_task(redis_client, processing_key, dedup_key, task):
    redis_client.lrem(processing_key, 1, task["_redis_payload"])
    redis_client.srem(dedup_key, get_queue_member(task))


def requeue_redis_task(redis_client, pending_key, processing_key, task, retry_count):
    redis_client.lrem(processing_key, 1, task["_redis_payload"])
    next_task = {
        "source_id": task["source_id"],
        "url": task["url"],
        "retry_count": retry_count,
    }
    redis_client.rpush(pending_key, json.dumps(next_task, ensure_ascii=False))


def flush_db_buffer(conn, buffered_items):
    if not buffered_items:
        return []
    payloads = [item["payload"] for item in buffered_items]
    try:
        upsert_results(conn, payloads)
        conn.commit()
        return [None] * len(buffered_items)
    except Exception as batch_exc:
        conn.rollback()
        errors = []
        for item in buffered_items:
            try:
                upsert_result(conn, item["payload"])
                conn.commit()
                errors.append(None)
            except Exception as row_exc:
                conn.rollback()
                errors.append(f"{type(batch_exc).__name__}: {batch_exc}; row={type(row_exc).__name__}: {row_exc}")
        return errors


def get_proxy_args(proxy_api_url, proxy_api_key, timeout):
    return argparse.Namespace(
        proxy_api_url=proxy_api_url,
        proxy_api_key=proxy_api_key,
        timeout=timeout,
    )


def get_thread_proxy(proxy_api_url, proxy_api_key, timeout, force_refresh=False):
    proxy_info = getattr(THREAD_PROXY_STATE, "proxy_info", None)
    proxy_use_count = getattr(THREAD_PROXY_STATE, "proxy_use_count", 0)
    if force_refresh or not proxy_info or proxy_use_count >= DEFAULT_PROXY_MAX_USES:
        proxy_info = fetch_proxy(get_proxy_args(proxy_api_url, proxy_api_key, timeout))
        THREAD_PROXY_STATE.proxy_info = proxy_info
        THREAD_PROXY_STATE.proxy_use_count = 0
    return proxy_info


def clear_thread_proxy():
    THREAD_PROXY_STATE.proxy_info = None
    THREAD_PROXY_STATE.proxy_use_count = 0


def capture_one(source_id, url, cookie, timeout, impersonate, proxy_api_url, proxy_api_key, force_refresh_proxy=False):
    headers = {
        "user-agent": DEFAULT_USER_AGENT,
        "accept-language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["cookie"] = cookie

    proxy_info = {
        "proxy_url": None,
        "proxy_out_ip": None,
        "proxy_fetch_id": None,
        "proxy_payload": None,
    }
    request_kwargs = {}
    proxy_info = get_thread_proxy(
        proxy_api_url=proxy_api_url,
        proxy_api_key=proxy_api_key,
        timeout=timeout,
        force_refresh=force_refresh_proxy,
    )
    THREAD_PROXY_STATE.proxy_use_count = getattr(THREAD_PROXY_STATE, "proxy_use_count", 0) + 1
    request_kwargs["proxies"] = {
        "http": proxy_info["proxy_url"],
        "https": proxy_info["proxy_url"],
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        impersonate=impersonate,
        **request_kwargs,
    )
    html = response.text
    initial_state_raw = extract_initial_state_raw(html)
    initial_state_json = None
    parsed_summary = None
    parse_error = None

    if initial_state_raw:
        try:
            state = json.loads(normalize_js_object_to_json(initial_state_raw))
            initial_state_json = json.dumps(state, ensure_ascii=False)
            parsed_summary = json.dumps(summarize_state(state), ensure_ascii=False)
        except Exception as exc:
            parse_error = str(exc)
    else:
        parse_error = "window.__INITIAL_STATE__ not found"

    return {
        "source_id": source_id,
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


def is_retryable_error(exc):
    message = str(exc)
    retryable_markers = [
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (52)",
        "curl: (56)",
        "SSL_ERROR_SYSCALL",
        "Connection timed out",
        "Operation timed out",
        "Proxy CONNECT aborted",
        "Could not connect",
    ]
    return any(marker in message for marker in retryable_markers)


def capture_with_retries(source_id, url, cookie, timeout, impersonate, proxy_api_url, proxy_api_key, max_retries, prior_retry_count):
    last_exc = None
    remaining_attempts = max(0, max_retries - prior_retry_count)
    if remaining_attempts == 0:
        raise RuntimeError(f"retry limit reached: {prior_retry_count}")
    for attempt in range(1, remaining_attempts + 1):
        try:
            payload = capture_one(
                source_id=source_id,
                url=url,
                cookie=cookie,
                timeout=timeout,
                impersonate=impersonate,
                proxy_api_url=proxy_api_url,
                proxy_api_key=proxy_api_key,
                force_refresh_proxy=(attempt > 1),
            )
            payload["retry_count"] = prior_retry_count + attempt - 1
            return payload
        except Exception as exc:
            last_exc = exc
            if is_retryable_error(exc):
                clear_thread_proxy()
            if attempt >= remaining_attempts or not is_retryable_error(exc):
                break
    raise last_exc


def parse_args():
    parser = argparse.ArgumentParser(description="Batch capture XHS initial state from xhs_url.")
    parser.add_argument("--limit", type=int, default=0, help="0 means process all pending rows")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=0, help="0 means auto-size based on concurrency")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--seed-batch-size", type=int, default=DEFAULT_SEED_BATCH_SIZE)
    parser.add_argument("--db-batch-size", type=int, default=DEFAULT_DB_BATCH_SIZE)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--cookie", default=os.getenv("XHS_COOKIE", ""))
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument(
        "--proxy-api-url",
        default=os.getenv(
            "PROXY_API_URL",
            "http://120.26.6.140:56311/api/v1.0/business/proxy_service/third/fetch"
            "?channel_id=48&business_name=tea&protocol=socks5&project_name=yyb_login"
            "&degraded=1&province=110000&count=1",
        ),
    )
    parser.add_argument(
        "--proxy-api-key",
        default=os.getenv("PROXY_API_KEY", "yyb_logina9ce8e49c4abe4ca3ae0593a5540da21"),
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        args.batch_size = max(args.concurrency * DEFAULT_BATCH_MULTIPLIER, args.concurrency)
    return args


def main():
    args = parse_args()
    redis_client = create_redis_client()
    conn = pymysql.connect(**load_db_config())
    processed = 0
    success = 0
    failed = 0
    started_at = time.time()
    recent_events = collections.deque()
    db_buffer = []
    target_limit = args.limit if args.limit > 0 else None
    try:
        ensure_result_table(conn)
        pk_column = get_source_pk_column(conn)
        pending_key = os.getenv("XHS_PENDING_QUEUE_KEY", DEFAULT_QUEUE_KEY)
        processing_key = os.getenv("XHS_PROCESSING_QUEUE_KEY", DEFAULT_PROCESSING_KEY)
        dedup_key = os.getenv("XHS_QUEUE_DEDUP_KEY", DEFAULT_DEDUP_KEY)
        pending_before = redis_client.llen(pending_key)
        processing_before = redis_client.llen(processing_key)
        print(
            json.dumps(
                {
                    "event": "startup_begin",
                    "source_table": SOURCE_TABLE,
                    "pk_column": pk_column,
                    "limit": args.limit,
                    "batch_size": args.batch_size,
                    "concurrency": args.concurrency,
                    "max_retries": args.max_retries,
                    "resume": args.resume,
                    "redis_pending_key": pending_key,
                    "redis_processing_key": processing_key,
                    "redis_dedup_key": dedup_key,
                    "pending_queue_len": pending_before,
                    "processing_queue_len": processing_before,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        recovered = 0
        if processing_before > 0:
            print(
                json.dumps(
                    {
                        "event": "recover_begin",
                        "processing_queue_len": processing_before,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            recovered = recover_processing_tasks(redis_client, pending_key, processing_key)
            print(
                json.dumps(
                    {
                        "event": "recover_done",
                        "recovered_processing": recovered,
                        "pending_queue_len": redis_client.llen(pending_key),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        seed_info = {"seeded": 0, "scanned": 0, "skipped": True}
        if redis_client.llen(pending_key) == 0 and redis_client.llen(processing_key) == 0:
            print(
                json.dumps(
                    {
                        "event": "seed_begin",
                        "seed_batch_size": args.seed_batch_size,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            seed_info = seed_pending_tasks(
                redis_client=redis_client,
                conn=conn,
                pk_column=pk_column,
                max_retries=args.max_retries,
                limit=args.limit,
                seed_batch_size=args.seed_batch_size,
                pending_key=pending_key,
                dedup_key=dedup_key,
            )
            seed_info["skipped"] = False
        print(
            json.dumps(
                {
                    "event": "startup",
                    "source_table": SOURCE_TABLE,
                    "pk_column": pk_column,
                    "limit": args.limit,
                    "batch_size": args.batch_size,
                    "concurrency": args.concurrency,
                    "max_retries": args.max_retries,
                    "resume": args.resume,
                    "redis_pending_key": pending_key,
                    "redis_processing_key": processing_key,
                    "redis_dedup_key": dedup_key,
                    "recovered_processing": recovered,
                    "seeded": seed_info["seeded"],
                    "scanned": seed_info["scanned"],
                    "seed_skipped": seed_info["skipped"],
                    "pending_queue_len": redis_client.llen(pending_key),
                    "processing_queue_len": redis_client.llen(processing_key),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        offset = args.offset
        while True:
            batch_started_at = time.time()
            if target_limit is not None and processed >= target_limit:
                break
            batch_limit = args.batch_size
            if target_limit is not None:
                batch_limit = min(batch_limit, target_limit - processed)
            print(
                json.dumps(
                    {
                        "event": "fetch_batch",
                        "batch_limit": batch_limit,
                        "processed": processed,
                        "pending_queue_len": redis_client.llen(pending_key),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            rows = pop_redis_tasks(redis_client, pending_key, processing_key, batch_limit)
            print(
                json.dumps(
                    {
                        "event": "batch_rows",
                        "rows": len(rows),
                        "processed": processed,
                        "pending_queue_len": redis_client.llen(pending_key),
                        "processing_queue_len": redis_client.llen(processing_key),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if not rows:
                print(
                    json.dumps(
                        {
                            "event": "no_pending_rows",
                            "processed": processed,
                            "success": success,
                            "failed": failed,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break
            futures = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                for row in rows:
                    url = row["url"].strip()
                    source_id = row["source_id"]
                    prior_retry_count = int(row.get("retry_count", 0) or 0)
                    futures[
                        executor.submit(
                            capture_with_retries,
                            source_id,
                            url,
                            args.cookie,
                            args.timeout,
                            args.impersonate,
                            args.proxy_api_url,
                            args.proxy_api_key,
                            args.max_retries,
                            prior_retry_count,
                        )
                    ] = {
                        "source_id": source_id,
                        "url": url,
                        "prior_retry_count": prior_retry_count,
                        "_redis_payload": row["_redis_payload"],
                    }

                for future in concurrent.futures.as_completed(futures):
                    meta = futures[future]
                    source_id = meta["source_id"]
                    url = meta["url"]
                    prior_retry_count = meta["prior_retry_count"]
                    payload = {"source_id": source_id, "url": url}
                    should_requeue = False
                    try:
                        payload.update(future.result())
                        if payload["parse_error"] is None:
                            success += 1
                        else:
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        payload.update(
                            {
                                "http_status": None,
                                "final_url": None,
                                "title": None,
                                "description": None,
                                "keywords": None,
                                "proxy_url": None,
                                "proxy_out_ip": None,
                                "proxy_fetch_id": None,
                                "proxy_payload": None,
                                "retry_count": min(args.max_retries, prior_retry_count + 1),
                                "initial_state_raw": None,
                                "initial_state_json": None,
                                "parsed_summary": None,
                                "parse_error": str(exc),
                            }
                        )
                    parse_error = payload["parse_error"]
                    retry_count = int(payload.get("retry_count", 0) or 0)
                    if parse_error is not None:
                        retryable = is_retryable_error(RuntimeError(parse_error))
                        if retryable and retry_count < args.max_retries:
                            should_requeue = True
                    db_buffer.append(
                        {
                            "meta": meta,
                            "payload": payload,
                            "should_requeue": should_requeue,
                        }
                    )
                    if len(db_buffer) >= args.db_batch_size:
                        write_errors = flush_db_buffer(conn, db_buffer)
                        for item, write_error in zip(db_buffer, write_errors):
                            meta = item["meta"]
                            payload = item["payload"]
                            should_requeue = item["should_requeue"]
                            final_parse_error = payload["parse_error"]
                            if write_error is None:
                                if final_parse_error is None:
                                    ack_redis_task(redis_client, processing_key, dedup_key, meta)
                                elif should_requeue:
                                    requeue_redis_task(
                                        redis_client=redis_client,
                                        pending_key=pending_key,
                                        processing_key=processing_key,
                                        task=meta,
                                        retry_count=int(payload.get("retry_count", 0) or 0),
                                    )
                                else:
                                    ack_redis_task(redis_client, processing_key, dedup_key, meta)
                            else:
                                final_parse_error = f"db_write_failed: {write_error}"
                                payload["parse_error"] = final_parse_error
                                payload["http_status"] = payload.get("http_status")
                            processed += 1
                            now = time.time()
                            recent_events.append((now, payload["parse_error"] is None))
                            cutoff = now - 60
                            while recent_events and recent_events[0][0] < cutoff:
                                recent_events.popleft()
                            recent_processed = len(recent_events)
                            recent_success = sum(1 for _, ok in recent_events if ok)
                            recent_failed = recent_processed - recent_success
                            elapsed_total = max(now - started_at, 0.001)
                            print(
                                json.dumps(
                                    {
                                        "processed": processed,
                                        "success": success,
                                        "failed": failed,
                                        "elapsed_sec": round(elapsed_total, 2),
                                        "avg_tps": round(processed / elapsed_total, 2),
                                        "avg_success_tps": round(success / elapsed_total, 2),
                                        "last_60s_processed": recent_processed,
                                        "last_60s_success": recent_success,
                                        "last_60s_failed": recent_failed,
                                        "url": meta["url"],
                                        "http_status": payload["http_status"],
                                        "proxy_out_ip": payload["proxy_out_ip"],
                                        "retry_count": payload.get("retry_count", 0),
                                        "parse_error": payload["parse_error"],
                                        "requeued": should_requeue and write_error is None,
                                        "db_write_error": write_error,
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            if args.sleep > 0:
                                time.sleep(args.sleep)
                        db_buffer = []
                if db_buffer:
                    write_errors = flush_db_buffer(conn, db_buffer)
                    for item, write_error in zip(db_buffer, write_errors):
                        meta = item["meta"]
                        payload = item["payload"]
                        should_requeue = item["should_requeue"]
                        if write_error is None:
                            if payload["parse_error"] is None:
                                ack_redis_task(redis_client, processing_key, dedup_key, meta)
                            elif should_requeue:
                                requeue_redis_task(
                                    redis_client=redis_client,
                                    pending_key=pending_key,
                                    processing_key=processing_key,
                                    task=meta,
                                    retry_count=int(payload.get("retry_count", 0) or 0),
                                )
                            else:
                                ack_redis_task(redis_client, processing_key, dedup_key, meta)
                        else:
                            payload["parse_error"] = f"db_write_failed: {write_error}"
                        processed += 1
                        now = time.time()
                        recent_events.append((now, payload["parse_error"] is None))
                        cutoff = now - 60
                        while recent_events and recent_events[0][0] < cutoff:
                            recent_events.popleft()
                        recent_processed = len(recent_events)
                        recent_success = sum(1 for _, ok in recent_events if ok)
                        recent_failed = recent_processed - recent_success
                        elapsed_total = max(now - started_at, 0.001)
                        print(
                            json.dumps(
                                {
                                    "processed": processed,
                                    "success": success,
                                    "failed": failed,
                                    "elapsed_sec": round(elapsed_total, 2),
                                    "avg_tps": round(processed / elapsed_total, 2),
                                    "avg_success_tps": round(success / elapsed_total, 2),
                                    "last_60s_processed": recent_processed,
                                    "last_60s_success": recent_success,
                                    "last_60s_failed": recent_failed,
                                    "url": meta["url"],
                                    "http_status": payload["http_status"],
                                    "proxy_out_ip": payload["proxy_out_ip"],
                                    "retry_count": payload.get("retry_count", 0),
                                    "parse_error": payload["parse_error"],
                                    "requeued": should_requeue and write_error is None,
                                    "db_write_error": write_error,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if args.sleep > 0:
                            time.sleep(args.sleep)
                    db_buffer = []
            batch_elapsed = max(time.time() - batch_started_at, 0.001)
            print(
                json.dumps(
                    {
                        "event": "batch_done",
                        "batch_rows": len(rows),
                        "processed": processed,
                        "success": success,
                        "failed": failed,
                        "batch_elapsed_sec": round(batch_elapsed, 2),
                        "batch_tps": round(len(rows) / batch_elapsed, 2),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        print(
            json.dumps(
                {
                    "event": "done",
                    "processed": processed,
                    "success": success,
                    "failed": failed,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "fatal",
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise
