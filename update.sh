#!/bin/sh
# Standalone updater for the README's docker run deployment on a Linux VPS.
set -eu
exec python3 -u - <<'PY'
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

NAME = 'telegram-forwarder-bot'
IMAGE = 'ghcr.io/homomimo/telegram-forwarder-bot-webui:latest'
SOCKET = '/var/run/docker.sock'
BACKUPS = Path('/root/telegram-forwarder/backups')
STAGED = NAME + '.next'
PREVIOUS = NAME + '.previous'
stop_requested = False


def docker(*args, include_stderr=False):
    # Inspect and application logs can contain credentials. Never echo their output on error.
    if stop_requested:
        raise KeyboardInterrupt
    result = subprocess.run(['docker', '--host', 'unix://' + SOCKET, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if include_stderr else subprocess.DEVNULL, start_new_session=True)
    if result.returncode:
        raise RuntimeError(f'docker {args[0]} 失败，请检查本机 Docker 服务、权限或网络。')
    return result.stdout


def inspect(kind, reference):
    return json.loads(docker(kind, 'inspect', reference))[0]


def environment(config):
    return dict(item.split('=', 1) for item in config.get('Env') or [])


def create_candidate(old, base, overrides, image_id):
    # Replay runtime overrides, not the previous image's CMD, PATH, version or labels.
    config = old['Config']
    payload = {key: value for key, value in config.items() if value != base.get(key)}
    if config['Hostname'] == old['Id'][:12]:
        payload.pop('Hostname', None)
    payload['Env'] = [f'{k}={v}' for k, v in overrides.items()]
    labels = base.get('Labels') or {}
    payload['Labels'] = {k: v for k, v in (config.get('Labels') or {}).items() if labels.get(k) != v}
    payload.update(Image=image_id, HostConfig=old['HostConfig'])
    # Use the local Engine create API so secrets never appear in argv or a temporary env file.
    version = docker('version', '--format', '{{.Server.APIVersion}}').decode().strip()
    url = f'http://localhost/v{version}/containers/create?name={STAGED}'
    result = subprocess.run(['curl', '--disable', '--silent', '--show-error', '--fail', '--noproxy', '*',
                             '--unix-socket', SOCKET, '-H', 'Content-Type: application/json',
                             '--data-binary', '@-', url], input=json.dumps(payload).encode(),
                            stdout=subprocess.PIPE, start_new_session=True)
    if result.returncode:
        raise RuntimeError('无法创建候选容器；原容器未停止。')
    return json.loads(result.stdout)['Id']


def comparable_host_config(container):
    host = container['HostConfig'].copy()
    # Docker can report an unset OOM-killer opt-out as null on a running container
    # and false on its newly created replacement. An explicit true is not equivalent.
    if host.get('OomKillDisable') is None:
        host['OomKillDisable'] = False
    return host


def data_directory(old):
    host = old['HostConfig']
    mounts = old['Mounts']
    networks = old['NetworkSettings']['Networks']
    ports = host.get('PortBindings') or {}
    if old['State']['Status'] != 'running' or host.get('AutoRemove') or host.get('ContainerIDFile'):
        raise RuntimeError('需要运行中的独立容器，不支持 --rm 或 --cidfile。')
    if host['RestartPolicy']['Name'] not in ('unless-stopped', 'no', ''):
        raise RuntimeError('请使用 README 的 unless-stopped 重启策略，避免停机备份期间自动启动。')
    if any(k.startswith('com.docker.compose.') for k in old['Config'].get('Labels') or {}):
        raise RuntimeError('此脚本仅更新 docker run 部署，不接管 Compose 管理的容器。')
    if (host['NetworkMode'] not in ('bridge', 'default') or set(networks) != {'bridge'}
            or any(networks['bridge'].get(k) for k in ('IPAMConfig', 'Aliases', 'DriverOpts', 'Links'))):
        raise RuntimeError('不支持自定义网络或多个网络，请用原部署方式更新。')
    if host.get('PublishAllPorts') or not ports.get('8765/tcp') or any(
        not binding['HostPort'] or binding['HostPort'] == '0'
        for entries in ports.values() for binding in entries or []
    ):
        raise RuntimeError('需要将容器 8765/tcp 映射到固定宿主机端口。')
    if (len(mounts) != 1 or mounts[0]['Type'] != 'bind' or mounts[0]['Destination'] != '/data'
            or not mounts[0]['RW'] or environment(old['Config']).get('RELAY_DATA') != '/data'):
        raise RuntimeError('仅支持将单个宿主机数据目录以可写模式绑定到 /data。')
    data = Path(mounts[0]['Source']).resolve(strict=True)
    backups = BACKUPS.resolve()
    if not (data / 'live').is_dir() or data == backups or data in backups.parents:
        raise RuntimeError('数据目录不正确，或备份目录位于数据目录内。')
    return data


def check_queue(data):
    journal = data / 'live' / 'message_map.sqlite3.queue.sqlite3'
    # mode=ro must not silently create an empty queue if a deployment uses another layout.
    db = sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute("SELECT 1 FROM jobs WHERE state != 'review' LIMIT 1").fetchone():
            raise RuntimeError('队列非空，未切换镜像。请稍后用 /queue 核对并重试。')
    finally:
        db.close()


def backup(data):
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=BACKUPS, prefix=time.strftime('%Y%m%d-%H%M%S-'),
                                     suffix='.tar.gz.partial', delete=False) as output:
        path = Path(output.name)
    try:
        subprocess.run(['tar', '--acls', '--xattrs', '--numeric-owner', '-czf', str(path), '-C', str(data), '.'],
                       check=True, start_new_session=True)
        archive = path.with_suffix('')
        path.rename(archive)
    except BaseException:
        if path.exists():
            path.unlink()
        raise
    print('停机备份：', archive)


# Probe only the loopback TLS endpoint inside this container; the service validates the HTTP Host header.
PROBE = '''import http.client, json, os, ssl
conn = http.client.HTTPSConnection('127.0.0.1', int(os.environ['PORT']), timeout=4,
                                   context=ssl._create_unverified_context())
conn.request('GET', '/api/auth/session', headers={'Host': os.environ['RELAY_VPS_IP']})
r = conn.getresponse()
raise SystemExit(0 if r.status == 200 and json.load(r).get('authenticated') is False else 1)
'''


def wait_ready(cid):
    deadline = time.monotonic() + 180
    started = inspect('container', cid)['State']['StartedAt']
    while time.monotonic() < deadline:
        state = inspect('container', cid)['State']
        if state['Status'] != 'running' or state['StartedAt'] != started:
            raise RuntimeError('容器在启动检查期间退出或重启。')
        logs = docker('logs', '--since', started, '--tail', '1000', cid, include_stderr=True)
        if '[启动/就绪] Bot 已启动'.encode() in logs:
            docker('exec', cid, 'python', '-c', PROBE)
            after = inspect('container', cid)['State']
            if after['Status'] == 'running' and after['StartedAt'] == started:
                return
            raise RuntimeError('容器在 WebUI 检查期间退出或重启。')
        time.sleep(2)
    raise RuntimeError('180 秒内未收到 Bot 就绪日志。')


def update():
    global stop_requested
    old = inspect('container', NAME)
    previous_id = old['Id']
    base = inspect('image', old['Image'])['Config']
    if (base.get('Labels') or {}).get('org.opencontainers.image.title') != 'Telegram Forwarder · Bot + WebUI':
        raise RuntimeError('同名容器并非本项目的镜像，未改动。')
    print('正在检查', IMAGE)
    docker('pull', IMAGE)
    image = inspect('image', IMAGE)
    names = docker('container', 'ls', '-a', '--format', '{{.Names}}').decode().splitlines()
    if STAGED in names or PREVIOUS in names:
        raise RuntimeError('发现上次更新留下的 .next 或 .previous 容器，请先检查，不会覆盖。')
    if old['Image'] == image['Id']:
        print('已是最新镜像，未停止容器。')
        return
    data = data_directory(old)
    if b'GNU tar' not in subprocess.check_output(['tar', '--version'], start_new_session=True):
        raise RuntimeError('需要 GNU tar 来保留备份的所有者和权限。')
    check_queue(data)
    defaults = environment(base)
    current = environment(old['Config'])
    if defaults.keys() - current.keys():
        raise RuntimeError('不支持显式移除镜像环境变量的部署。')
    overrides = {k: v for k, v in current.items() if defaults.get(k) != v}
    cid = create_candidate(old, base, overrides, image['Id'])
    stopping_old = False
    try:
        candidate = inspect('container', cid)
        if candidate['Mounts'] != old['Mounts']:
            raise RuntimeError('候选容器的挂载（Mounts）与原容器不一致，取消更新。')
        old_host = comparable_host_config(old)
        new_host = comparable_host_config(candidate)
        if old_host != new_host:
            fields = sorted(k for k in old_host.keys() | new_host.keys()
                            if k not in old_host or k not in new_host or old_host[k] != new_host[k])
            raise RuntimeError('候选容器运行参数不一致，取消更新。差异字段：' + ', '.join(fields))
        if environment(candidate['Config']) != {**environment(image['Config']), **overrides}:
            raise RuntimeError('候选容器的环境配置不符，取消更新。')
        stopping_old = True
        docker('stop', '-t', '60', previous_id)
        stopped = inspect('container', previous_id)['State']
        if stopped['Status'] != 'exited' or stopped['ExitCode'] == 137:
            raise RuntimeError('旧容器未正常停止，取消更新。')
        check_queue(data)  # Catch admissions between the first check and graceful shutdown.
        backup(data)
        docker('rename', previous_id, PREVIOUS)
        docker('rename', cid, NAME)
        docker('start', cid)
        wait_ready(cid)
    except BaseException:
        # Even if a start/rename request was interrupted, act on IDs, not on a guessed name.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        stop_requested = False
        if inspect('container', cid)['State']['Running']:
            docker('stop', '-t', '60', cid)
        docker('rm', cid)  # Must succeed before the old process can access /data again.
        if stopping_old:
            print('更新未完成，尝试恢复旧容器（不还原数据）。', file=sys.stderr)
            old = inspect('container', previous_id)
            if old['Name'] != '/' + NAME:
                docker('rename', previous_id, NAME)
            if not old['State']['Running']:
                docker('start', previous_id)
                wait_ready(previous_id)
            print('旧容器已恢复运行；未覆盖当前数据。')
        raise
    print('更新成功，Bot 与 WebUI 已就绪。镜像 ID：', image['Id'])
    docker('rm', previous_id)  # Cleanup failure must not roll back a verified update.


def interrupted(*_):
    # Finish any in-flight Engine request before deciding which container to roll back.
    global stop_requested
    stop_requested = True


try:
    if os.geteuid() != 0:
        raise RuntimeError('请在 Linux VPS 上以 root 运行。')
    os.umask(0o077)
    with open('/run/telegram-forwarder-bot-update.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, interrupted)
        update()
except BlockingIOError:
    sys.exit('另一个更新正在进行，请勿重复运行。')
except KeyboardInterrupt:
    sys.exit('更新已中断。请用 docker ps -a 核对容器状态。')
except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
    sys.exit(f'更新脚本：{exc} 请用 docker ps -a 核对状态。')
PY
