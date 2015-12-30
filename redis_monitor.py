#!/usr/bin/env python
#-*- coding:utf8 -*-


import re
import json
import time
import socket
import logging
import traceback

import redis
import requests

logging.basicConfig(
    filename = '/tmp/redis_monitor-%s.log' % time.strftime('%Y-%m-%d'),
    filemode = 'a',
    level = logging.NOTSET,
    format = '%(asctime)s - %(levelname)s: %(message)s'
)


IGNORE_KEYS = [
    # Server
    "redis_version",
    "redis_git_sha1",
    "redis_git_dirty",
    "redis_build_id",
    "redis_mode",
    "os",
    "arch_bits",
    "multiplexing_api",
    "gcc_version",
    "process_id",
    "run_id",
    "tcp_port",
    "uptime_in_seconds",
    "uptime_in_days",
    "hz",
    "lru_clock",
    "config_file",

    # Memory
    "used_memory_human",
    "used_memory_peak_human",
    "mem_allocator",
]

COUNTER_METRICS = [
    "total_connections_received",
    "rejected_connections",
    "keyspace_hits",
    "keyspace_misses",
    "total_commands_processed",
    "total_net_input_bytes",
    "total_net_output_bytes",
    "expired_keys",
    "evicted_keys",
    "used_cpu_sys",
    "used_cpu_user",
    "slowlog_len",
]

class RedisMonitor(object):
    """
    load.1min                           |all(#3)>10                     |Redis服务器过载，处理能力下降|
    cpu.idle                            |all(#3)<10                     |CPU idle过低，处理能力下降|
    df.bytes.free.percent               |all(#3)<20                     |磁盘可用空间百分比低于20%，影响从库RDB和AOF持久化|
    mem.memfree.percent                 |all(#3)<15                     |内存剩余低于15%，Redis有OOM killer和使用swap的风险|
    mem.swapfree.percent                |all(#3)<80                     |使用20% swap,Redis性能下降或OOM风险|
    net.if.out.bytes                    |all(#3)>94371840               |网络出口流量超90MB,影响Redis响应|
    net.if.in.bytes                     |all(#3)>94371840               |网络入口流量超90MB,影响Redis响应|
    disk.io.util                        |all(#3)>90                     |磁盘IO可能存负载，影响从库持久化和阻塞写|
    redis.alive                         |all(#2)=0                      |Redis实例存活有问题，可能不可用|
    used_memory                         |all(#2)>32212254720            |单实例使用30G，建议拆分扩容；对fork卡停，full_sync时长都有明显性能影响|
    used_memory_pct                     |all(#3)>85                     |(存储场景)使用内存达85%,存储场景会写入失败|
    mem_fragmentation_ratio             |all(#3)>2                      |内存碎片过高(如果实例比较小，这个指标可能比较大，不实用)|
    connected_clients                   |all(#3)>5000                   |客户端连接数超5000|
    connected_clients_pct               |all(#3)>85                     |客户端连接数占最大连接数超85%|
    rejected_connections                |all(#1)>0                      |连接数达到maxclients后，创建新连接失败|
    total_connections_received                                          |每秒新创建连接数超5000，对Redis性能有明显影响，常见于PHP短连接场景|
    master_link_status                  |all(#1)=0                      |主从同步断开；会全量同步，HA/备份/读写分离数据最终一致性受影响|
    slave_read_only                     |all(#1)=0                      |从库非只读状态|
    repl_backlog_active                 |all(#1)=0                      |repl_backlog关闭，对网络闪断场景不能psync|
    keys                                |all(#1)>50000000               |keyspace key总数5千万，建议拆分扩容|
    instantaneous_ops_per_sec           |all(#2)>30000                  |整体QPS 30000,建议拆分扩容|
    slowlog_len                         |all(#1)>10                     |1分钟中内，出现慢查询个数(一般Redis命令响应大于1ms，记录为slowlog)|
    latest_fork_usec                    |all(#1)>1000000                |最近一次fork耗时超1秒(其间Redis不能响应任何请求)|
    keyspace_hit_ratio                  |all(#2)<80                     |命中率低于80%|
    cluster_state                       |all(#1)=0                      |Redis集群处理于FAIL状态，不能提供读写|
    cluster_slots_assigned              |all(#1)<16384                  |keyspace的所有数据槽未被全部指派，集群处理于FAIL状态|
    cluster_slots_fail                  |all(#1)>0                      |集群中有槽处于失败，集群处理于FAIL状态|
    """
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.hostname = socket.gethostname()
        self.password = password
        self.tags = 'port=%s' % port

    def _collect(self):
        redis_info = {}
        r = redis.StrictRedis(host=self.host,
                              port=self.port,
                              password=self.password
                             )
        if r.ping():
            redis_info['alive'] = 1
        else:
            redis_info['alive'] = 0
            return redis_info

        info = r.info()

        info_cmd_stat = None

        if info['redis_version'] > '2.6':
            info_cmd_stat = r.info('commandstats')

        redis_max_clients = 0

        try:
            max_clients_list = r.execute_command('config get maxclients')
            if len(max_clients_list) == 2:
                redis_max_clients = int(max_clients_list[1])
        except:
            max_clients_list = r.execute_command('config_command get maxclients')
            if len(max_clients_list) == 2:
                redis_max_clients = int(max_clients_list[1])


        if redis_max_clients > 0:
            redis_info['connected_clients_percent'] = info['connected_clients']/float(redis_max_clients)*100

        if info_cmd_stat:
            for cmd_key in info_cmd_stat:
                redis_info[cmd_key] = info_cmd_stat[cmd_key]['calls']

        all_db_size = 0

        for key in info:
            if key in IGNORE_KEYS:
                continue
            elif key == 'rdb_last_bgsave_status':
                redis_info[key] = 1 if info[key] == 'ok' else 0
            elif key == 'aof_last_bgrewrite_status':
                redis_info[key] = 1 if info[key] == 'ok' else 0
            elif key == 'aof_last_write_status':
                redis_info[key] = 1 if info[key] == 'ok' else 0
            elif key == 'role':
                redis_info[key] = 1 if info[key] == 'master' else 0
            elif key == 'master_link_status':
                redis_info[key] = 1 if info[key] == 'up' else 0
            elif re.match('db[0-15]+', key):
                all_db_size = all_db_size + int(info[key]["keys"])
            else:
                redis_info[key] = info[key]

        redis_info['slowlog_len'] = r.slowlog_len()

        return redis_info
    

    def run(self):
        redis_info = self._collect()
        payload = []
        ts = int(time.time())

        for key in redis_info:
            metric = 'redis.%s' % key.replace('_', '.')
            if key in COUNTER_METRICS:
                item = {
                    "endpoint": self.hostname,
                    "metric": metric, 
                    "tags": self.tags, 
                    "timestamp": ts, 
                    "value": redis_info[key], 
                    "step": 60, 
                    "counterType": "COUNTER"
                }
            else:
                item = {
                    "endpoint": self.hostname, 
                    "metric": metric,
                    "tags": self.tags,
                    "timestamp": ts,
                    "value": redis_info[key],
                    "step": 60, "counterType": "GAUGE"
                }
            payload.append(item)
        return payload

def push(payload):
    if payload:
        try:
            r = requests.post("http://127.0.0.1:1988/v1/push", data=json.dumps(payload))
            logging.info('push data status: %s' % r.text)
        except:
            logging.warn('push data status failed, Exception: %s' % traceback.format_exc())

def main():
    redis_instances = [
        {'host': '127.0.0.1', 'port': 6379, 'password': None},
    ]
    payload = []
    for instance in redis_instances:
        try:
            host = instance['host']
            port = instance['port']
            password = instance['password']
            redis_monitor = RedisMonitor(host, port, password)
            payload.extend(redis_monitor.run())
        except:
            logging.info('Exception: %s' %traceback.format_exc())
    logging.info('payload length: %s' % len(payload))
    push(payload)

if __name__ == '__main__':
    start = time.time()
    main()
    logging.info('Cost: %s' % (time.time() - start))
