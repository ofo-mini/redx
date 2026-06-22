#!/usr/bin/env python3
import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from mt_callback_client import DEFAULT_ESEREP_MT_KEY, MtClient
from xhs_initial_state_capture import ensure_mongo_indexes, insert_result_mongo, insert_results_mongo, open_mongo_collection
from xhs_queue_common import (
    create_redis_client,
    get_queue_member,
    get_queue_names,
    load_env_file,
    load_result_redis_config,
    pop_processing_items,
    pop_queue_items,
)


DEFAULT_DB_BATCH_SIZE = 100
DEFAULT_IDLE_SLEEP = 3.0
DEFAULT_MT_CALLBACK_TIMEOUT = 30
DEFAULT_MT_CALLBACK_RETRIES = 3
DEFAULT_MT_RECENT_DAYS = 7
MISSING_PAGE_TITLE = "小红书 - 你访问的页面不见了"


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
    term_width = shutil.get_terminal_size((140, 20)).columns
    max_width = max(80, term_width - 1)
    if len(line) > max_width:
        line = line[: max_width - 3] + "..."
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


def load_mt_callback_config():
    load_env_file()
    enabled = os.getenv("MT_CALLBACK_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    return {
        "enabled": enabled,
        "key": os.getenv("MT_CALLBACK_KEY", DEFAULT_ESEREP_MT_KEY),
        "timeout": int(os.getenv("MT_CALLBACK_TIMEOUT", str(DEFAULT_MT_CALLBACK_TIMEOUT))),
        "retries": int(os.getenv("MT_CALLBACK_RETRIES", str(DEFAULT_MT_CALLBACK_RETRIES))),
        "recent_days": int(os.getenv("MT_CALLBACK_RECENT_DAYS", str(DEFAULT_MT_RECENT_DAYS))),
    }


def open_mt_client(config):
    if not config["enabled"]:
        return None
    return MtClient(timeout=config["timeout"])


def decode_json_text(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def build_mt_item_id(payload):
    if payload.get("source_id") is not None:
        return f"xhs-source-{payload['source_id']}"
    if payload.get("note_id"):
        return f"xhs-note-{payload['note_id']}"
    return "xhs-url-" + hashlib.md5(payload["url"].encode("utf-8")).hexdigest()


def extract_mt_note_payload(initial_state_json, note_id=None):
    state = decode_json_text(initial_state_json)
    if isinstance(state, dict) and note_id:
        note_detail_map = ((state.get("note") or {}).get("noteDetailMap") or {})
        note_payload = note_detail_map.get(str(note_id))
        if note_payload is not None:
            return note_payload
    return state


def build_mt_payload(payload):
    return extract_mt_note_payload(payload.get("initial_state_json"), note_id=payload.get("note_id"))


def parse_last_update_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return parse_last_update_time(int(raw))
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                return parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def should_send_mt_by_time(payload, recent_days):
    last_update_dt = parse_last_update_time(payload.get("lastUpdateTime"))
    if last_update_dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    return last_update_dt >= cutoff


def is_missing_page_payload(payload):
    return (payload.get("title") or "").strip() == MISSING_PAGE_TITLE


def split_buffered_items(buffered_items):
    persist_items = []
    skipped_items = []
    for item in buffered_items:
        if is_missing_page_payload(item["payload"]):
            skipped_items.append(item)
        else:
            persist_items.append(item)
    return persist_items, skipped_items


def send_mt_callback(mt_client, mt_config, payload):
    if mt_client is None:
        return None
    if is_missing_page_payload(payload):
        return "skipped_missing_page"
    if not should_send_mt_by_time(payload, mt_config["recent_days"]):
        return "skipped_old_post"
    item_id = build_mt_item_id(payload)
    data_payload = build_mt_payload(payload)
    retries = max(1, int(mt_config.get("retries", DEFAULT_MT_CALLBACK_RETRIES)))
    last_result = None
    for attempt in range(1, retries + 1):
        try:
            result = mt_client.send_via_eserep(
                key=mt_config["key"],
                item_id=item_id,
                data=data_payload,
            )
        except Exception as exc:
            result = {
                "mode": "eserep",
                "ok": False,
                "trace_id": None,
                "request_url": None,
                "request_host": None,
                "request_path": None,
                "param": None,
                "error": str(exc),
                "attempt": attempt,
            }
        last_result = result
        if result.get("ok") or result.get("skipped"):
            return None
        if attempt < retries:
            time.sleep(min(1.0, 0.2 * attempt))
    emit_log({
        "event": "mt_callback_trace",
        "item_id": item_id,
        "key": mt_config["key"],
        "trace_id": last_result.get("trace_id"),
        "ok": last_result.get("ok"),
        "request_url": last_result.get("request_url"),
        "request_host": last_result.get("request_host"),
        "request_path": last_result.get("request_path"),
        "param": last_result.get("param"),
        "attempts": retries,
        "error": last_result.get("error"),
    })
    return json.dumps(last_result, ensure_ascii=False)


def run_mt_callbacks(mt_client, mt_config, payloads, max_workers):
    if mt_client is None or not payloads:
        return 0, 0, 0
    callback_success = 0
    callback_failed = 0
    skipped_old_post = 0
    total = len(payloads)
    max_workers = max(1, min(max_workers, total))
    update_progress("writer", st="mt", total=total, workers=max_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(send_mt_callback, mt_client, mt_config, payload) for payload in payloads]
        for idx, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            callback_error = future.result()
            if callback_error is None or callback_error == "skipped_missing_page":
                callback_success += 1
            elif callback_error == "skipped_old_post":
                skipped_old_post += 1
            else:
                callback_failed += 1
            if idx == total or idx % 10 == 0:
                update_progress("writer", st="mt", done=idx, total=total, ok=callback_success, fail=callback_failed, old=skipped_old_post)
    return callback_success, callback_failed, skipped_old_post


def flush_db_buffer(collection, buffered_items, mt_client, mt_config, mt_callback_workers):
    if not buffered_items:
        return [], 0, 0, 0, 0
    persist_items, skipped_items = split_buffered_items(buffered_items)
    skipped_missing_page = len(skipped_items)
    if not persist_items:
        return [None] * len(buffered_items), 0, 0, 0, skipped_missing_page
    payloads = [item["payload"] for item in persist_items]
    update_progress("writer", st="db", rows=len(persist_items), miss=skipped_missing_page)
    try:
        insert_results_mongo(collection, payloads)
        update_progress("writer", st="db_done", rows=len(persist_items), miss=skipped_missing_page)
        errors_by_id = {id(item): None for item in persist_items}
        callback_success, callback_failed, skipped_old_post = run_mt_callbacks(mt_client, mt_config, payloads, mt_callback_workers)
    except Exception as batch_exc:
        emit_log({"event": "mongo_write_fallback", "error": str(batch_exc), "rows": len(persist_items)})
        errors_by_id = {}
        succeeded_payloads = []
        for item in persist_items:
            try:
                insert_result_mongo(collection, item["payload"])
                succeeded_payloads.append(item["payload"])
                errors_by_id[id(item)] = None
            except Exception as row_exc:
                errors_by_id[id(item)] = f"{type(batch_exc).__name__}: {batch_exc}; row={type(row_exc).__name__}: {row_exc}"
        callback_success, callback_failed, skipped_old_post = run_mt_callbacks(mt_client, mt_config, succeeded_payloads, mt_callback_workers)
    write_errors = []
    for item in buffered_items:
        if item in skipped_items:
            write_errors.append(None)
        else:
            write_errors.append(errors_by_id.get(id(item)))
    return write_errors, callback_success, callback_failed, skipped_old_post, skipped_missing_page


def finalize_results(task_redis_client, result_redis_client, queues, envelopes, write_errors):
    task_pipe = task_redis_client.pipeline(transaction=False)
    result_pipe = result_redis_client.pipeline(transaction=False)
    finalized = 0
    requeued = 0
    failed = 0
    for envelope, write_error in zip(envelopes, write_errors):
        if write_error is not None:
            failed += 1
            continue
        task = envelope["task"]
        should_requeue = envelope["should_requeue"]
        if should_requeue:
            task_pipe.rpush(queues["fetch_pending"], json.dumps(task, ensure_ascii=False))
            requeued += 1
        else:
            task_pipe.srem(queues["dedup"], get_queue_member(task))
        if not envelope.get("_from_processing"):
            result_pipe.rpop(queues["result_processing"])
        finalized += 1
    if finalized:
        task_pipe.execute()
        result_pipe.execute()
    return {
        "finalized": finalized,
        "requeued": requeued,
        "failed": failed,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Consume parsed XHS results from Redis and batch write to MongoDB.")
    parser.add_argument("--db-batch-size", type=int, default=DEFAULT_DB_BATCH_SIZE)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--idle-sleep", type=float, default=DEFAULT_IDLE_SLEEP)
    parser.add_argument("--mt-callback-workers", type=int, default=0)
    parser.add_argument("--send-external", dest="send_external", action="store_true")
    parser.add_argument("--no-send-external", dest="send_external", action="store_false")
    parser.set_defaults(send_external=None)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.mt_callback_workers <= 0:
        args.mt_callback_workers = args.db_batch_size
    task_redis_client = create_redis_client()
    result_redis_client = create_redis_client(load_result_redis_config)
    queues = get_queue_names()
    mongo_client, collection = open_mongo_collection()
    mt_config = load_mt_callback_config()
    if args.send_external is not None:
        mt_config["enabled"] = args.send_external
    mt_client = open_mt_client(mt_config)
    ensure_mongo_indexes(collection)
    written = 0
    db_success = 0
    write_failed = 0
    callback_success = 0
    callback_failed = 0
    skipped_old_post = 0
    skipped_missing_page = 0
    started_at = time.time()
    try:
        emit_log(
            {
                "event": "startup",
                "queues": queues,
                "mt_callback_enabled": mt_client is not None,
                "mt_callback_key": mt_config["key"] if mt_client is not None else None,
                "mt_callback_mode": "eserep" if mt_client is not None else None,
                "mt_callback_workers": args.mt_callback_workers if mt_client is not None else 0,
                "result_pending": result_redis_client.llen(queues["result_pending"]),
                "result_processing": result_redis_client.llen(queues["result_processing"]),
            }
        )
        while True:
            rows = pop_processing_items(result_redis_client, queues["result_processing"], args.db_batch_size)
            source = "result_processing"
            if not rows:
                rows = pop_queue_items(result_redis_client, queues["result_pending"], queues["result_processing"], args.db_batch_size)
                source = "result_pending"
            result_pending = result_redis_client.llen(queues["result_pending"])
            result_processing = result_redis_client.llen(queues["result_processing"])
            if not rows:
                elapsed = max(time.time() - started_at, 0.001)
                update_progress(
                    "writer",
                    st="idle",
                    dbok=db_success,
                    mtok=callback_success,
                    miss=skipped_missing_page,
                    old=skipped_old_post,
                    fail=write_failed,
                    tps=round(db_success / elapsed, 2),
                    sleep=args.idle_sleep,
                    pend=result_pending,
                    proc=result_processing,
                )
                time.sleep(args.idle_sleep)
                continue
            update_progress("writer", st="batch", src=source, rows=len(rows), pend=result_pending, proc=result_processing)
            buffered_items = [{"task": row["task"], "payload": row["payload"], "should_requeue": row["should_requeue"], "_redis_payload": row["_redis_payload"], "_from_processing": row.get("_from_processing", False)} for row in rows]
            write_errors, batch_callback_success, batch_callback_failed, batch_skipped_old_post, batch_skipped_missing_page = flush_db_buffer(collection, buffered_items, mt_client, mt_config, args.mt_callback_workers)
            finalize_stats = finalize_results(task_redis_client, result_redis_client, queues, buffered_items, write_errors)
            written += len(buffered_items)
            batch_db_failed = sum(1 for err in write_errors if err is not None)
            write_failed += batch_db_failed
            db_success += len(buffered_items) - batch_skipped_missing_page - batch_db_failed
            callback_success += batch_callback_success
            callback_failed += batch_callback_failed
            skipped_old_post += batch_skipped_old_post
            skipped_missing_page += batch_skipped_missing_page
            elapsed = max(time.time() - started_at, 0.001)
            update_progress(
                "writer",
                st="done",
                src=source,
                rows=len(buffered_items),
                dbok=db_success,
                mtok=callback_success,
                miss=skipped_missing_page,
                old=skipped_old_post,
                fail=write_failed,
                tps=round(db_success / elapsed, 2),
                pend=result_redis_client.llen(queues["result_pending"]),
                proc=result_redis_client.llen(queues["result_processing"]),
            )
            if args.sleep > 0:
                time.sleep(args.sleep)
    finally:
        finish_progress()
        mongo_client.close()


if __name__ == "__main__":
    main()
