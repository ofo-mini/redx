#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import re
import threading
import time
import sys
from urllib.parse import urlparse

import pymysql
from curl_cffi import requests

from xhs_initial_state_capture import (
    DEFAULT_IMPERSONATE,
    DEFAULT_USER_AGENT,
    extract_note_id_from_url,
    extract_initial_state_raw,
    extract_meta,
    fetch_proxy,
    load_db_config,
    get_result_state_map,
    normalize_js_object_to_json,
    open_mongo_collection,
    ensure_mongo_indexes,
    summarize_state,
)
from xhs_queue_common import (
    create_redis_client,
    get_queue_member,
    get_queue_names,
    load_result_redis_config,
    pop_queue_items,
    recover_list_queue,
)


SOURCE_TABLE = "xhs_url"
DEFAULT_BATCH_MULTIPLIER = 10
DEFAULT_PROXY_MAX_USES = 10
DEFAULT_SEED_BATCH_SIZE = 5000
DEFAULT_QUEUE_TARGET_SIZE = 50000
DEFAULT_QUEUE_LOW_WATERMARK = 10000
DEFAULT_RESULT_QUEUE_MAX_SIZE = 500000
DEFAULT_RESULT_QUEUE_WAIT = 2.0
DEFAULT_LOG_SUCCESS_EVERY = 0
DEFAULT_MAX_RETRIES = 5
THREAD_PROXY_STATE = threading.local()
RETRYABLE_ERROR_SQL_REGEX = (
    "curl: \\((7|28|35|52|56)\\)|"
    "SSL_ERROR_SYSCALL|"
    "Connection timed out|"
    "Operation timed out|"
    "Proxy CONNECT aborted|"
    "Could not connect"
)
RETRYABLE_ERROR_RE = re.compile(RETRYABLE_ERROR_SQL_REGEX)
URL_CANDIDATE_RE = re.compile(r"https?://[^\s,]+")
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_STATE = {"last_line_len": 0}


def _clear_progress_line_locked():
    last_len = _PROGRESS_STATE["last_line_len"]
    if last_len > 0:
        sys.stdout.write("\r" + (" " * last_len) + "\r")
        _PROGRESS_STATE["last_line_len"] = 0


def emit_log(payload):
    with _PROGRESS_LOCK:
        _clear_progress_line_locked()
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def update_progress(prefix, **fields):
    parts = [prefix]
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    line = " | ".join(parts)
    with _PROGRESS_LOCK:
        padded = line
        if len(line) < _PROGRESS_STATE["last_line_len"]:
            padded += " " * (_PROGRESS_STATE["last_line_len"] - len(line))
        sys.stdout.write("\r" + padded)
        sys.stdout.flush()
        _PROGRESS_STATE["last_line_len"] = len(line)


def finish_progress():
    with _PROGRESS_LOCK:
        if _PROGRESS_STATE["last_line_len"] > 0:
            sys.stdout.write("\n")
            sys.stdout.flush()
            _PROGRESS_STATE["last_line_len"] = 0


NON_RETRYABLE_ERROR_MARKERS = (
    "curl: (3)",
    "URL rejected: Bad hostname",
    "No host part in the URL",
    "unsupported URL scheme",
)
SILENT_FINAL_ERROR_MARKERS = (
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
)


class CaptureRequestError(RuntimeError):
    def __init__(self, message, proxy_info=None):
        super().__init__(message)
        self.proxy_info = proxy_info or {}


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
    for row in rows:
        if row["COLUMN_KEY"] == "PRI":
            return row["COLUMN_NAME"]
    return "id"


def fetch_source_rows(conn, pk_column, limit, after_source_id=None):
    after_clause = ""
    params = []
    if after_source_id is not None:
        after_clause = f"AND s.`{pk_column}` > %s"
        params.append(after_source_id)
    sql = f"""
    SELECT s.`{pk_column}` AS source_id, s.`url`
    FROM `{SOURCE_TABLE}` s
    WHERE s.`url` IS NOT NULL
      AND TRIM(s.`url`) <> ''
      {after_clause}
    ORDER BY s.`{pk_column}`
    LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def is_xiaohongshu_url(url):
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")


def normalize_source_url(raw_url):
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    candidates = URL_CANDIDATE_RE.findall(raw_url)
    if not candidates and raw_url.startswith(("http://", "https://")):
        candidates = [raw_url]
    for candidate in candidates:
        cleaned = candidate.strip().rstrip(".,;)")
        if is_xiaohongshu_url(cleaned):
            return cleaned
    return None


def seed_pending_tasks(redis_client, conn, result_collection, pk_column, max_retries, limit, seed_batch_size, pending_key, dedup_key, seed_cursor_key, target_add_count):
    current_cursor = redis_client.get(seed_cursor_key)
    last_source_id = int(current_cursor) if current_cursor not in (None, "") else None
    seeded = 0
    scanned = 0
    remaining = limit if limit and limit > 0 else None
    wrapped = False
    while seeded < target_add_count:
        fetch_limit = min(seed_batch_size, max(target_add_count - seeded, 1))
        if remaining is not None:
            fetch_limit = min(fetch_limit, remaining)
        if fetch_limit <= 0:
            break
        rows = fetch_source_rows(conn, pk_column, fetch_limit, after_source_id=last_source_id)
        if not rows and last_source_id is not None and not wrapped:
            last_source_id = None
            wrapped = True
            continue
        if not rows:
            break
        state_map = get_result_state_map(result_collection, [row["source_id"] for row in rows], RETRYABLE_ERROR_RE)
        eligible_rows = []
        for row in rows:
            normalized_url = normalize_source_url(row["url"])
            if not normalized_url:
                continue
            row["url"] = normalized_url
            state = state_map.get(row["source_id"])
            if not state:
                row["retry_count"] = 0
                eligible_rows.append(row)
                continue
            if state["has_success"]:
                continue
            if state["has_retryable_error"] and state["max_retry_count"] < max_retries:
                row["retry_count"] = state["max_retry_count"]
                eligible_rows.append(row)
        new_tasks = []
        pipe = redis_client.pipeline(transaction=False)
        for row in eligible_rows:
            task = {
                "source_id": row["source_id"],
                "url": row["url"],
                "retry_count": int(row.get("retry_count", 0) or 0),
            }
            pipe.sadd(dedup_key, get_queue_member(task))
        results = pipe.execute()
        for index, row in enumerate(eligible_rows):
            if results[index] == 1:
                new_tasks.append(
                    json.dumps(
                        {
                            "source_id": row["source_id"],
                            "url": row["url"],
                            "retry_count": int(row.get("retry_count", 0) or 0),
                        },
                        ensure_ascii=False,
                    )
                )
                seeded += 1
        if new_tasks:
            redis_client.rpush(pending_key, *new_tasks)
        scanned += len(rows)
        update_progress("fetch", stage="seed", seeded=seeded, scanned=scanned)
        last_source_id = rows[-1]["source_id"]
        if remaining is not None:
            remaining -= len(rows)
    if last_source_id is not None:
        redis_client.set(seed_cursor_key, last_source_id)
    return {"seeded": seeded, "scanned": scanned, "seed_cursor": last_source_id}


def get_proxy_args(proxy_api_url, proxy_api_key, timeout):
    return argparse.Namespace(proxy_api_url=proxy_api_url, proxy_api_key=proxy_api_key, timeout=timeout)


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


def get_thread_proxy_snapshot():
    proxy_info = getattr(THREAD_PROXY_STATE, "proxy_info", None) or {}
    return {
        "proxy_url": proxy_info.get("proxy_url"),
        "proxy_out_ip": proxy_info.get("proxy_out_ip"),
        "proxy_fetch_id": proxy_info.get("proxy_fetch_id"),
        "proxy_payload": proxy_info.get("proxy_payload"),
    }


def capture_one(source_id, url, cookie, timeout, impersonate, proxy_api_url, proxy_api_key, force_refresh_proxy=False):
    headers = {"user-agent": DEFAULT_USER_AGENT, "accept-language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["cookie"] = cookie
    proxy_info = get_thread_proxy(proxy_api_url, proxy_api_key, timeout, force_refresh=force_refresh_proxy)
    THREAD_PROXY_STATE.proxy_use_count = getattr(THREAD_PROXY_STATE, "proxy_use_count", 0) + 1
    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        impersonate=impersonate,
        proxies={"http": proxy_info["proxy_url"], "https": proxy_info["proxy_url"]},
    )
    html = response.text
    initial_state_raw = extract_initial_state_raw(html)
    initial_state_json = None
    parsed_summary = None
    parse_error = None
    if initial_state_raw:
        try:
            state = json.loads(normalize_js_object_to_json(initial_state_raw))
            initial_state_json = state
            parsed_summary = json.dumps(summarize_state(state), ensure_ascii=False)
        except Exception as exc:
            parse_error = str(exc)
    else:
        parse_error = "window.__INITIAL_STATE__ not found"
    return {
        "source_id": source_id,
        "note_id": extract_note_id_from_url(url),
        "http_status": response.status_code,
        "final_url": response.url,
        "title": extract_meta(html, "title"),
        "description": extract_meta(html, "description"),
        "keywords": extract_meta(html, "keywords"),
        "proxy_url": proxy_info["proxy_url"],
        "proxy_out_ip": proxy_info["proxy_out_ip"],
        "proxy_fetch_id": proxy_info["proxy_fetch_id"],
        "proxy_payload": proxy_info["proxy_payload"],
        "initial_state_json": initial_state_json,
        "parsed_summary": parsed_summary,
        "parse_error": parse_error,
    }


def is_non_retryable_error(exc):
    message = str(exc)
    return any(marker in message for marker in NON_RETRYABLE_ERROR_MARKERS)


def should_suppress_final_error_log(parse_error):
    if not parse_error:
        return False
    return any(marker in parse_error for marker in SILENT_FINAL_ERROR_MARKERS)


def capture_with_retries(source_id, url, cookie, timeout, impersonate, proxy_api_url, proxy_api_key, max_retries, prior_retry_count):
    last_exc = None
    remaining_attempts = max(0, max_retries - prior_retry_count)
    if remaining_attempts == 0:
        raise RuntimeError(f"retry limit reached: {prior_retry_count}")
    for attempt in range(1, remaining_attempts + 1):
        try:
            payload = capture_one(
                source_id,
                url,
                cookie,
                timeout,
                impersonate,
                proxy_api_url,
                proxy_api_key,
                force_refresh_proxy=(attempt > 1),
            )
            payload["retry_count"] = prior_retry_count + attempt - 1
            return payload
        except Exception as exc:
            proxy_snapshot = get_thread_proxy_snapshot()
            last_exc = CaptureRequestError(str(exc), proxy_snapshot)
            clear_thread_proxy()
            if attempt >= remaining_attempts or is_non_retryable_error(exc):
                break
    raise last_exc


def enqueue_result(task_redis_client, result_redis_client, result_pending_key, fetch_processing_key, meta, envelope):
    result_redis_client.rpush(result_pending_key, json.dumps(envelope, ensure_ascii=False))
    task_redis_client.lrem(fetch_processing_key, 1, meta["_redis_payload"])


def requeue_fetch_task(task_redis_client, fetch_pending_key, fetch_processing_key, meta, retry_count):
    pipe = task_redis_client.pipeline(transaction=False)
    pipe.rpush(
        fetch_pending_key,
        json.dumps(
            {
                "source_id": meta["source_id"],
                "url": meta["url"],
                "retry_count": retry_count,
            },
            ensure_ascii=False,
        ),
    )
    pipe.lrem(fetch_processing_key, 1, meta["_redis_payload"])
    pipe.execute()


def wait_for_result_queue_capacity(result_redis_client, queues, result_queue_max_size, result_queue_wait):
    while True:
        queued = result_redis_client.llen(queues["result_pending"]) + result_redis_client.llen(queues["result_processing"])
        if queued < result_queue_max_size:
            return queued
        update_progress("fetch", stage="result_queue_wait", queued=queued, result_queue_max_size=result_queue_max_size, sleep=result_queue_wait)
        time.sleep(result_queue_wait)


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch XHS pages and push parsed results into Redis.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--seed-batch-size", type=int, default=DEFAULT_SEED_BATCH_SIZE)
    parser.add_argument("--queue-target-size", type=int, default=DEFAULT_QUEUE_TARGET_SIZE)
    parser.add_argument("--queue-low-watermark", type=int, default=DEFAULT_QUEUE_LOW_WATERMARK)
    parser.add_argument("--result-queue-max-size", type=int, default=DEFAULT_RESULT_QUEUE_MAX_SIZE)
    parser.add_argument("--result-queue-wait", type=float, default=DEFAULT_RESULT_QUEUE_WAIT)
    parser.add_argument("--cookie", default="")
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument("--proxy-api-url", default="")
    parser.add_argument("--proxy-api-key", default="")
    parser.add_argument("--log-success-every", type=int, default=DEFAULT_LOG_SUCCESS_EVERY)
    args = parser.parse_args()
    if args.batch_size <= 0:
        args.batch_size = max(args.concurrency * DEFAULT_BATCH_MULTIPLIER, args.concurrency)
    if not args.proxy_api_url:
        args.proxy_api_url = (
            "http://api.xiequ.cn/VAD/GetIp.aspx?act=get&uid=177346"
            "&vkey=A3ED2598E7897DEFBA39A28839E3178B&num=200&time=30"
            "&plat=1&re=0&type=1&so=1&ow=1&spl=1&addr=&db=1"
        )
    if not args.proxy_api_key:
        args.proxy_api_key = ""
    return args


def main():
    args = parse_args()
    task_redis_client = create_redis_client()
    result_redis_client = create_redis_client(load_result_redis_config)
    conn = pymysql.connect(**load_db_config())
    mongo_client, result_collection = open_mongo_collection()
    queues = get_queue_names()
    processed = 0
    success = 0
    failed = 0
    started_at = time.time()
    target_limit = args.limit if args.limit > 0 else None
    try:
        pk_column = get_source_pk_column(conn)
        ensure_mongo_indexes(result_collection)
        emit_log({"event": "startup_begin", "queues": queues, "concurrency": args.concurrency, "batch_size": args.batch_size})
        recovered_fetch = 0
        if task_redis_client.llen(queues["fetch_processing"]) > 0:
            recovered_fetch = recover_list_queue(task_redis_client, queues["fetch_processing"], queues["fetch_pending"])
        recovered_results = 0
        if result_redis_client.llen(queues["result_processing"]) > 0:
            recovered_results = recover_list_queue(result_redis_client, queues["result_processing"], queues["result_pending"])
        seed_info = {"seeded": 0, "scanned": 0, "skipped": True, "seed_cursor": task_redis_client.get(queues["seed_cursor"])}
        fetch_pending_len = task_redis_client.llen(queues["fetch_pending"])
        fetch_processing_len = task_redis_client.llen(queues["fetch_processing"])
        if fetch_pending_len + fetch_processing_len < args.queue_target_size:
            need = args.queue_target_size - (fetch_pending_len + fetch_processing_len)
            seed_info = seed_pending_tasks(
                task_redis_client,
                conn,
                result_collection,
                pk_column,
                args.max_retries,
                args.limit,
                args.seed_batch_size,
                queues["fetch_pending"],
                queues["dedup"],
                queues["seed_cursor"],
                need,
            )
            seed_info["skipped"] = False
        emit_log({"event": "startup", "recovered_fetch": recovered_fetch, "recovered_results": recovered_results, **seed_info})
        while True:
            if target_limit is not None and processed >= target_limit:
                break
            fetch_pending_len = task_redis_client.llen(queues["fetch_pending"])
            fetch_processing_len = task_redis_client.llen(queues["fetch_processing"])
            if fetch_pending_len + fetch_processing_len <= args.queue_low_watermark:
                refill_need = max(args.queue_target_size - (fetch_pending_len + fetch_processing_len), 0)
                if refill_need > 0:
                    update_progress("fetch", stage="seed_begin", refill_need=refill_need, pending=fetch_pending_len, processing=fetch_processing_len)
                    seed_info = seed_pending_tasks(
                        task_redis_client,
                        conn,
                        result_collection,
                        pk_column,
                        args.max_retries,
                        args.limit,
                        args.seed_batch_size,
                        queues["fetch_pending"],
                        queues["dedup"],
                        queues["seed_cursor"],
                        refill_need,
                    )
                    update_progress("fetch", stage="seed_done", seeded=seed_info.get("seeded"), scanned=seed_info.get("scanned"), seed_cursor=seed_info.get("seed_cursor"))
            batch_limit = args.batch_size if target_limit is None else min(args.batch_size, target_limit - processed)
            rows = pop_queue_items(task_redis_client, queues["fetch_pending"], queues["fetch_processing"], batch_limit)
            update_progress("fetch", stage="batch", rows=len(rows), pending=task_redis_client.llen(queues["fetch_pending"]), result_pending=result_redis_client.llen(queues["result_pending"]))
            if not rows:
                break
            batch_started_at = time.time()
            futures = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                for row in rows:
                    normalized_url = normalize_source_url(row["url"])
                    if not normalized_url:
                        task_redis_client.lrem(queues["fetch_processing"], 1, row["_redis_payload"])
                        continue
                    row["url"] = normalized_url
                    futures[executor.submit(
                        capture_with_retries,
                        row["source_id"],
                        row["url"],
                        args.cookie,
                        args.timeout,
                        args.impersonate,
                        args.proxy_api_url,
                        args.proxy_api_key,
                        args.max_retries,
                        int(row.get("retry_count", 0) or 0),
                    )] = row
                for future in concurrent.futures.as_completed(futures):
                    meta = futures[future]
                    payload = {"source_id": meta["source_id"], "note_id": extract_note_id_from_url(meta["url"]), "url": meta["url"]}
                    should_requeue = False
                    is_success = False
                    try:
                        payload.update(future.result())
                        is_success = payload["parse_error"] is None
                    except Exception as exc:
                        retry_count = min(args.max_retries, int(meta.get("retry_count", 0) or 0) + 1)
                        proxy_snapshot = getattr(exc, "proxy_info", None) or {}
                        payload.update(
                            {
                                "http_status": None,
                                "final_url": None,
                                "title": None,
                                "description": None,
                                "keywords": None,
                                "proxy_url": proxy_snapshot["proxy_url"],
                                "proxy_out_ip": proxy_snapshot["proxy_out_ip"],
                                "proxy_fetch_id": proxy_snapshot["proxy_fetch_id"],
                                "proxy_payload": proxy_snapshot["proxy_payload"],
                                "retry_count": retry_count,
                                "initial_state_json": None,
                                "parsed_summary": None,
                                "parse_error": str(exc),
                            }
                        )
                    if payload["parse_error"] is not None:
                        if not is_non_retryable_error(RuntimeError(payload["parse_error"])) and int(payload.get("retry_count", 0) or 0) < args.max_retries:
                            should_requeue = True
                    if is_success:
                        success += 1
                    elif not should_requeue:
                        failed += 1
                    if should_requeue:
                        requeue_fetch_task(
                            task_redis_client,
                            queues["fetch_pending"],
                            queues["fetch_processing"],
                            meta,
                            int(payload.get("retry_count", 0) or 0),
                        )
                    else:
                        envelope = {
                            "task": {
                                "source_id": meta["source_id"],
                                "note_id": extract_note_id_from_url(meta["url"]),
                                "url": meta["url"],
                                "retry_count": int(payload.get("retry_count", 0) or 0),
                            },
                            "payload": payload,
                            "should_requeue": False,
                        }
                        wait_for_result_queue_capacity(
                            result_redis_client,
                            queues,
                            args.result_queue_max_size,
                            args.result_queue_wait,
                        )
                        enqueue_result(task_redis_client, result_redis_client, queues["result_pending"], queues["fetch_processing"], meta, envelope)
                    processed += 1
                    elapsed = max(time.time() - started_at, 0.001)
                    should_log_row = (
                        payload["parse_error"] is not None
                        and not should_requeue
                        and not should_suppress_final_error_log(payload["parse_error"])
                    )
                    if not should_log_row and args.log_success_every > 0 and success % args.log_success_every == 0:
                        should_log_row = True
                    if should_log_row:
                        emit_log(
                            {
                                "event": "row_done",
                                "processed": processed,
                                "success": success,
                                "failed": failed,
                                "avg_tps": round(processed / elapsed, 2),
                                "avg_success_tps": round(success / elapsed, 2),
                                "url": meta["url"],
                                "http_status": payload["http_status"],
                                "proxy_url": payload["proxy_url"],
                                "proxy_out_ip": payload["proxy_out_ip"],
                                "retry_count": payload.get("retry_count", 0),
                                "parse_error": payload["parse_error"],
                                "requeued": should_requeue,
                            }
                        )
            batch_elapsed = max(time.time() - batch_started_at, 0.001)
            update_progress(
                "fetch",
                stage="done",
                batch_rows=len(rows),
                processed=processed,
                success=success,
                failed=failed,
                batch_elapsed_sec=round(batch_elapsed, 2),
                batch_tps=round(len(rows) / batch_elapsed, 2),
            )
        emit_log({"event": "done", "processed": processed, "success": success, "failed": failed})
    finally:
        finish_progress()
        conn.close()
        mongo_client.close()


if __name__ == "__main__":
    main()
