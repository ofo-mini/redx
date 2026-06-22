#!/usr/bin/env python3
import json
import os

import redis


DEFAULT_FETCH_PENDING_KEY = "xhs:initial_state:pending"
DEFAULT_FETCH_PROCESSING_KEY = "xhs:initial_state:processing"
DEFAULT_RESULT_PENDING_KEY = "xhs:initial_state:result_pending"
DEFAULT_RESULT_PROCESSING_KEY = "xhs:initial_state:result_processing"
DEFAULT_DEDUP_KEY = "xhs:initial_state:queued"
DEFAULT_SEED_CURSOR_KEY = "xhs:initial_state:seed_cursor"


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


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


def load_result_redis_config():
    load_env_file()
    redis_url = os.getenv("REDIS_URL_RESULT")
    if redis_url:
        return {"from_url": redis_url}
    config = {
        "host": os.getenv("REDIS_HOST_RESULT", os.getenv("REDIS_HOST", "127.0.0.1")),
        "port": int(os.getenv("REDIS_PORT_RESULT", os.getenv("REDIS_PORT", "6379"))),
        "db": int(os.getenv("REDIS_DB_RESULT", os.getenv("REDIS_DB", "0"))),
        "decode_responses": True,
        "socket_timeout": float(os.getenv("REDIS_SOCKET_TIMEOUT_RESULT", os.getenv("REDIS_SOCKET_TIMEOUT", "5"))),
        "socket_connect_timeout": float(os.getenv("REDIS_CONNECT_TIMEOUT_RESULT", os.getenv("REDIS_CONNECT_TIMEOUT", "5"))),
    }
    password = os.getenv("REDIS_PASSWORD_RESULT", os.getenv("REDIS_PASSWORD"))
    if password:
        config["password"] = password
    username = os.getenv("REDIS_USERNAME_RESULT", os.getenv("REDIS_USERNAME"))
    if username:
        config["username"] = username
    return config


def create_redis_client(config_loader=load_redis_config):
    config = config_loader()
    if "from_url" in config:
        return redis.Redis.from_url(
            config["from_url"],
            decode_responses=True,
            socket_timeout=config.get("socket_timeout", 5),
            socket_connect_timeout=config.get("socket_connect_timeout", 5),
        )
    return redis.Redis(**config)


def get_queue_names():
    return {
        "fetch_pending": os.getenv("XHS_PENDING_QUEUE_KEY", DEFAULT_FETCH_PENDING_KEY),
        "fetch_processing": os.getenv("XHS_PROCESSING_QUEUE_KEY", DEFAULT_FETCH_PROCESSING_KEY),
        "result_pending": os.getenv("XHS_RESULT_PENDING_QUEUE_KEY", DEFAULT_RESULT_PENDING_KEY),
        "result_processing": os.getenv("XHS_RESULT_PROCESSING_QUEUE_KEY", DEFAULT_RESULT_PROCESSING_KEY),
        "dedup": os.getenv("XHS_QUEUE_DEDUP_KEY", DEFAULT_DEDUP_KEY),
        "seed_cursor": os.getenv("XHS_SEED_CURSOR_KEY", DEFAULT_SEED_CURSOR_KEY),
    }


def get_queue_member(task):
    source_id = task.get("source_id")
    return str(source_id) if source_id is not None else task["url"]


def recover_list_queue(redis_client, source_key, target_key):
    recovered = 0
    while redis_client.llen(source_key) > 0:
        moved = redis_client.rpoplpush(source_key, target_key)
        if moved is None:
            break
        recovered += 1
    return recovered


def pop_queue_items(redis_client, pending_key, processing_key, limit):
    items = []
    for _ in range(limit):
        payload = redis_client.rpoplpush(pending_key, processing_key)
        if payload is None:
            break
        item = json.loads(payload)
        item["_redis_payload"] = payload
        items.append(item)
    return items


def pop_processing_items(redis_client, processing_key, limit):
    items = []
    for _ in range(limit):
        payload = redis_client.rpop(processing_key)
        if payload is None:
            break
        item = json.loads(payload)
        item["_redis_payload"] = payload
        item["_from_processing"] = True
        items.append(item)
    return items
