#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import time

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

from pymongo import MongoClient

from xhs_fetch_worker import (
    DEFAULT_BATCH_MULTIPLIER,
    DEFAULT_LOG_SUCCESS_EVERY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RESULT_QUEUE_MAX_SIZE,
    DEFAULT_RESULT_QUEUE_WAIT,
    DEFAULT_SEED_BATCH_SIZE,
    RETRYABLE_ERROR_RE,
    capture_with_retries,
    emit_log,
    ensure_mongo_indexes,
    extract_note_id_from_url,
    finish_progress,
    get_result_state_map,
    is_non_retryable_error,
    normalize_source_url,
    should_suppress_final_error_log,
    update_progress,
    wait_for_result_queue_capacity,
)
from xhs_initial_state_capture import (
    DEFAULT_IMPERSONATE,
    load_mongodb_database_name,
    load_mongodb_uri,
    load_env_file,
    open_mongo_collection,
)
from xhs_queue_common import create_redis_client, get_queue_names, load_result_redis_config, recover_list_queue


DEFAULT_SOURCE_COLLECTION = "xhs_url"
DEFAULT_RUNTIME_STATE_COLLECTION = "xhs_runtime_state"
DEFAULT_RUNTIME_CURSOR_KEY_PREFIX = "xhs_fetch_worker_mongo"


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch XHS pages directly from MongoDB xhs_url and push results into result Redis.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--seed-batch-size", type=int, default=DEFAULT_SEED_BATCH_SIZE)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--result-queue-max-size", type=int, default=DEFAULT_RESULT_QUEUE_MAX_SIZE)
    parser.add_argument("--result-queue-wait", type=float, default=DEFAULT_RESULT_QUEUE_WAIT)
    parser.add_argument("--cookie", default="")
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument("--proxy-api-url", default="")
    parser.add_argument("--proxy-api-key", default="")
    parser.add_argument("--log-success-every", type=int, default=DEFAULT_LOG_SUCCESS_EVERY)
    parser.add_argument("--source-db", default="")
    parser.add_argument("--source-collection", default="")
    parser.add_argument("--runtime-state-collection", default="")
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


def open_source_mongo_collection(source_db_name="", source_collection_name=""):
    load_env_file()
    client = MongoClient(load_mongodb_uri())
    database = client[source_db_name or os.getenv("MONGODB_SOURCE_DB", load_mongodb_database_name())]
    collection = database[source_collection_name or os.getenv("MONGODB_SOURCE_COLLECTION", DEFAULT_SOURCE_COLLECTION)]
    return client, collection


def open_runtime_state_collection(source_mongo_client, source_db_name, runtime_state_collection_name=""):
    load_env_file()
    database = source_mongo_client[source_db_name]
    collection_name = runtime_state_collection_name or os.getenv("MONGODB_RUNTIME_STATE_COLLECTION", DEFAULT_RUNTIME_STATE_COLLECTION)
    return database[collection_name]


def build_runtime_cursor_key(source_collection):
    return f"{DEFAULT_RUNTIME_CURSOR_KEY_PREFIX}:{source_collection.database.name}:{source_collection.name}"


def detect_source_key_field(collection):
    if collection.find_one({"source_id": {"$exists": True, "$ne": None}}) is not None:
        return "source_id"
    if collection.find_one({"id": {"$exists": True, "$ne": None}}) is not None:
        return "id"
    return "_id"


def decode_cursor_value(value, cursor_field):
    if value in (None, ""):
        return None
    if cursor_field == "_id":
        if ObjectId is None:
            return None
        try:
            return ObjectId(str(value))
        except Exception:
            return None
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except Exception:
            return value
    return value


def encode_cursor_value(value):
    if value is None:
        return None
    return str(value)


def load_runtime_cursor(runtime_collection, runtime_key, cursor_field):
    row = runtime_collection.find_one({"_id": runtime_key}, {"cursor": 1}) or {}
    return decode_cursor_value(row.get("cursor"), cursor_field)


def save_runtime_cursor(runtime_collection, runtime_key, cursor_value):
    runtime_collection.update_one(
        {"_id": runtime_key},
        {
            "$set": {
                "cursor": encode_cursor_value(cursor_value),
                "updated_at": int(time.time()),
            }
        },
        upsert=True,
    )


def extract_source_id(doc):
    if doc.get("source_id") is not None:
        return doc.get("source_id")
    if doc.get("id") is not None:
        return doc.get("id")
    return str(doc.get("_id"))


def fetch_source_rows(collection, limit, cursor_field, after_value=None):
    query = {"url": {"$exists": True, "$ne": None}}
    if after_value is not None:
        query[cursor_field] = {"$gt": after_value}
    projection = {"_id": 1, "id": 1, "source_id": 1, "url": 1}
    cursor = collection.find(query, projection, no_cursor_timeout=False).sort(cursor_field, 1).limit(limit)
    return list(cursor)


def collect_batch_rows(source_collection, result_collection, cursor_field, start_cursor_value, max_retries, target_count, scan_chunk_size):
    cursor_value = start_cursor_value
    scanned = 0
    batch_rows = []
    exhausted = False
    while len(batch_rows) < target_count:
        fetch_limit = min(scan_chunk_size, max(target_count - len(batch_rows), 1))
        rows = fetch_source_rows(source_collection, fetch_limit, cursor_field, after_value=cursor_value)
        if not rows:
            exhausted = True
            break
        scanned += len(rows)
        cursor_value = rows[-1].get(cursor_field)
        normalized_rows = []
        source_ids = []
        for doc in rows:
            normalized_url = normalize_source_url(doc.get("url"))
            if not normalized_url:
                continue
            source_id = extract_source_id(doc)
            if source_id is None:
                continue
            row = {
                "source_id": source_id,
                "url": normalized_url,
            }
            normalized_rows.append(row)
            source_ids.append(source_id)
        state_map = get_result_state_map(result_collection, source_ids, RETRYABLE_ERROR_RE)
        for row in normalized_rows:
            state = state_map.get(row["source_id"])
            if not state:
                row["retry_count"] = 0
                batch_rows.append(row)
            elif state["has_success"]:
                continue
            elif state["has_retryable_error"] and state["max_retry_count"] < max_retries:
                row["retry_count"] = state["max_retry_count"]
                batch_rows.append(row)
            if len(batch_rows) >= target_count:
                break
    return batch_rows, cursor_value, scanned, exhausted


def enqueue_result_direct(result_redis_client, result_pending_key, envelope):
    result_redis_client.rpush(result_pending_key, json.dumps(envelope, ensure_ascii=False))


def main():
    args = parse_args()
    result_redis_client = create_redis_client(load_result_redis_config)
    source_mongo_client, source_collection = open_source_mongo_collection(args.source_db, args.source_collection)
    runtime_collection = open_runtime_state_collection(
        source_mongo_client,
        source_collection.database.name,
        args.runtime_state_collection,
    )
    result_mongo_client, result_collection = open_mongo_collection()
    cursor_field = detect_source_key_field(source_collection)
    runtime_key = build_runtime_cursor_key(source_collection)
    queues = get_queue_names()
    processed = 0
    success = 0
    failed = 0
    scanned = 0
    started_at = time.time()
    target_limit = args.limit if args.limit > 0 else None
    try:
        ensure_mongo_indexes(result_collection)
        recovered_results = 0
        if result_redis_client.llen(queues["result_processing"]) > 0:
            recovered_results = recover_list_queue(result_redis_client, queues["result_processing"], queues["result_pending"])
        cursor_value = load_runtime_cursor(runtime_collection, runtime_key, cursor_field)
        emit_log(
            {
                "event": "startup",
                "source": "mongodb_direct",
                "concurrency": args.concurrency,
                "batch_size": args.batch_size,
                "scan_chunk_size": args.seed_batch_size,
                "source_db": source_collection.database.name,
                "source_collection": source_collection.name,
                "source_cursor_field": cursor_field,
                "runtime_state_collection": runtime_collection.name,
                "cursor": encode_cursor_value(cursor_value),
                "recovered_results": recovered_results,
                "result_pending": result_redis_client.llen(queues["result_pending"]),
                "result_processing": result_redis_client.llen(queues["result_processing"]),
            }
        )
        while True:
            if target_limit is not None and processed >= target_limit:
                break
            batch_target = args.batch_size if target_limit is None else min(args.batch_size, target_limit - processed)
            rows, next_cursor_value, batch_scanned, exhausted = collect_batch_rows(
                source_collection,
                result_collection,
                cursor_field,
                cursor_value,
                args.max_retries,
                batch_target,
                args.seed_batch_size,
            )
            scanned += batch_scanned
            if next_cursor_value != cursor_value or exhausted:
                save_runtime_cursor(runtime_collection, runtime_key, None if exhausted else next_cursor_value)
            cursor_value = None if exhausted else next_cursor_value
            update_progress(
                "fetch_mongo",
                stage="batch",
                rows=len(rows),
                scanned=scanned,
                cursor=encode_cursor_value(cursor_value),
                result_pending=result_redis_client.llen(queues["result_pending"]),
            )
            if not rows:
                break
            batch_started_at = time.time()
            futures = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                for row in rows:
                    futures[
                        executor.submit(
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
                        )
                    ] = row
                for future in concurrent.futures.as_completed(futures):
                    meta = futures[future]
                    payload = {
                        "source_id": meta["source_id"],
                        "note_id": extract_note_id_from_url(meta["url"]),
                        "url": meta["url"],
                    }
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
                                "proxy_url": proxy_snapshot.get("proxy_url"),
                                "proxy_out_ip": proxy_snapshot.get("proxy_out_ip"),
                                "proxy_fetch_id": proxy_snapshot.get("proxy_fetch_id"),
                                "proxy_payload": proxy_snapshot.get("proxy_payload"),
                                "retry_count": retry_count,
                                "initial_state_json": None,
                                "parsed_summary": None,
                                "parse_error": str(exc),
                            }
                        )
                        is_success = False
                    if is_success:
                        success += 1
                    else:
                        failed += 1
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
                    enqueue_result_direct(result_redis_client, queues["result_pending"], envelope)
                    processed += 1
                    elapsed = max(time.time() - started_at, 0.001)
                    should_log_row = (
                        payload["parse_error"] is not None
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
                                "scanned": scanned,
                                "avg_tps": round(processed / elapsed, 2),
                                "avg_success_tps": round(success / elapsed, 2),
                                "url": meta["url"],
                                "http_status": payload["http_status"],
                                "proxy_url": payload["proxy_url"],
                                "proxy_out_ip": payload["proxy_out_ip"],
                                "retry_count": payload.get("retry_count", 0),
                                "parse_error": payload["parse_error"],
                                "requeued": False,
                            }
                        )
            batch_elapsed = max(time.time() - batch_started_at, 0.001)
            update_progress(
                "fetch_mongo",
                stage="done",
                batch_rows=len(rows),
                scanned=scanned,
                processed=processed,
                success=success,
                failed=failed,
                batch_elapsed_sec=round(batch_elapsed, 2),
                batch_tps=round(len(rows) / batch_elapsed, 2),
                cursor=encode_cursor_value(cursor_value),
            )
            if exhausted:
                break
        emit_log(
            {
                "event": "done",
                "source": "mongodb_direct",
                "processed": processed,
                "success": success,
                "failed": failed,
                "scanned": scanned,
                "cursor": encode_cursor_value(cursor_value),
            }
        )
    finally:
        finish_progress()
        source_mongo_client.close()
        result_mongo_client.close()


if __name__ == "__main__":
    main()
