# Network Architecture

## 当前链路

1. `xhs_fetch_worker.py`
   - 读取 MySQL `qq.rwlb.rds.aliyuncs.com:3306`
   - 读取任务 Redis `REDIS_HOST`
   - 写入结果 Redis `REDIS_HOST_RESULT`
   - 调用代理接口获取出口代理
   - 通过代理请求 `www.xiaohongshu.com`

2. `xhs_db_writer.py`
   - 读取结果 Redis `REDIS_HOST_RESULT`
   - 写入 MongoDB `MONGODB_HOST`
   - 写回任务 Redis `REDIS_HOST` 做队列收尾
   - 调用 MT 回传接口

## 外部依赖

- MySQL
  - 用途：源 URL 表 `xhs_url`
  - 地址：`qq.rwlb.rds.aliyuncs.com:3306`

- 代理接口
  - 地址：
    `http://api.xiequ.cn/VAD/GetIp.aspx?act=get&uid=177346&vkey=A3ED2598E7897DEFBA39A28839E3178B&num=200&time=30&plat=1&re=0&type=1&so=1&ow=1&spl=1&addr=&db=1`

- 目标站
  - `https://www.xiaohongshu.com`

- 任务 Redis
  - 配置：`.env` 的 `REDIS_HOST`

- 结果 Redis
  - 配置：`.env` 的 `REDIS_HOST_RESULT`

- MongoDB
  - 配置：`.env` 的 `MONGODB_HOST`
  - 默认库名：`data`
  - 默认集合：`xhs_initial_state_capture`

- MT 回传
  - 默认 key：`2viqbGfbeBVnbBv`
  - 默认走 `eserep`
  - 目标域名：`www.mxc2w35pzy.com`
  - 路径：`/p/j/eserep`

## 数据方向

```text
MySQL(xhs_url)
  -> task Redis
  -> fetch worker
  -> result Redis
  -> db writer
  -> MongoDB(xhs_initial_state_capture)
  -> MT eserep(data=initial_state_json)
```

## 队列说明

- 任务队列
  - `xhs:initial_state:pending`
  - `xhs:initial_state:processing`

- 结果队列
  - `xhs:initial_state:result_pending`
  - `xhs:initial_state:result_processing`

- 去重与游标
  - `xhs:initial_state:queued`
  - `xhs:initial_state:seed_cursor`
