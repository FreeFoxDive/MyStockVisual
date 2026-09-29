#!/bin/bash
# 宿主机内存与容器 OOM 告警。
#
# 容器里的 memguard 看不到其他服务占了多少内存, 容器被杀掉之后也发不出消息。
# 这一层跑在宿主机上, 走同一套 ntfy (visual/.env 的 NTFY_URL / NTFY_TOPIC /
# NTFY_USER / NTFY_PASSWORD)。
#
#   ./scripts/host_memwatch.sh check     # 每分钟一次: MemAvailable / Swap
#   ./scripts/host_memwatch.sh events    # 常驻: docker events 监听 OOM 与重启风暴
#
# cron:
#   * * * * * /path/to/visual/scripts/host_memwatch.sh check
# systemd (timer 调 check; events 用一个简单 service 常驻):
#   [Service]
#   ExecStart=/path/to/visual/scripts/host_memwatch.sh events
#   Restart=always
#
# 阈值 (可用环境变量覆盖):
#   HOST_MEM_AVAILABLE_MB=120   可用内存低于此值告警
#   HOST_SWAP_USED_PCT=60       swap 已用超过此百分比告警
#   HOST_MEM_COOLDOWN_SEC=1800  同一类告警最短间隔
#   HOST_WATCH_CONTAINER=stock-visual
#   HOST_RESTART_WINDOW_SEC=600
#   HOST_RESTART_MAX=3          窗口内容器退出 (die) 次数超过此值 → 重启风暴

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${HOST_MEMWATCH_ENV:-$SCRIPT_DIR/../.env}"
STATE_DIR="${HOST_MEMWATCH_STATE:-/tmp/host_memwatch}"
AVAIL_MB="${HOST_MEM_AVAILABLE_MB:-120}"
SWAP_PCT="${HOST_SWAP_USED_PCT:-60}"
COOLDOWN="${HOST_MEM_COOLDOWN_SEC:-1800}"
CONTAINER="${HOST_WATCH_CONTAINER:-stock-visual}"
WINDOW="${HOST_RESTART_WINDOW_SEC:-600}"
RESTART_MAX="${HOST_RESTART_MAX:-3}"

mkdir -p "$STATE_DIR"

load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
    case "$key" in
      NTFY_URL|NTFY_TOPIC|NTFY_USER|NTFY_PASSWORD)
        if [[ -z "${!key:-}" ]]; then
          export "$key=$val"
        fi
        ;;
    esac
  done < "$ENV_FILE"
}

ntfy_send() {
  local title="$1" text="$2"
  load_env
  if [[ -z "${NTFY_URL:-}" ]]; then
    echo "host_memwatch: 未配置 NTFY_URL, 跳过推送: $title" >&2
    return 0
  fi
  # 与 ntfy.py 一致: URL 自带 path 时 path 即 topic, 否则拼 NTFY_TOPIC
  local url="${NTFY_URL%/}"
  local rest="${url#*://}"
  if [[ "$rest" != */* ]]; then
    if [[ -z "${NTFY_TOPIC:-}" ]]; then
      echo "host_memwatch: NTFY_URL 无 topic 且未配置 NTFY_TOPIC, 跳过推送: $title" >&2
      return 0
    fi
    url="$url/$NTFY_TOPIC"
  fi
  if [[ -n "${NTFY_USER:-}" ]]; then
    curl -fsS --max-time 10 -u "$NTFY_USER:${NTFY_PASSWORD:-}" \
      -H "Title: $title" -H "Priority: high" \
      --data-binary "$text" "$url" >/dev/null || true
  else
    curl -fsS --max-time 10 \
      -H "Title: $title" -H "Priority: high" \
      --data-binary "$text" "$url" >/dev/null || true
  fi
}

cooled() {
  local kind="$1" now stamp
  now="$(date +%s)"
  stamp="$STATE_DIR/$kind.ts"
  if [[ -f "$stamp" ]]; then
    local prev
    prev="$(cat "$stamp" 2>/dev/null || echo 0)"
    if (( now - prev < COOLDOWN )); then
      return 1
    fi
  fi
  echo "$now" > "$stamp"
  return 0
}

mem_kb() {
  awk -v key="$1" '$1 == key":" { print $2; exit }' /proc/meminfo
}

cmd_check() {
  local avail swap_total swap_free swap_used pct now
  avail="$(mem_kb MemAvailable)"
  swap_total="$(mem_kb SwapTotal)"
  swap_free="$(mem_kb SwapFree)"
  now="$(date '+%F %T')"
  avail="${avail:-0}"
  swap_total="${swap_total:-0}"
  swap_free="${swap_free:-0}"
  local avail_mb=$(( avail / 1024 ))
  if (( swap_total > 0 )); then
    swap_used=$(( swap_total - swap_free ))
    pct=$(( swap_used * 100 / swap_total ))
  else
    pct=0
  fi
  if (( avail_mb < AVAIL_MB )); then
    if cooled "low-mem"; then
      ntfy_send "宿主机内存不足" "$now MemAvailable ${avail_mb}MB < ${AVAIL_MB}MB (swap 已用 ${pct}%)"
    fi
  fi
  if (( pct > SWAP_PCT )); then
    if cooled "swap"; then
      ntfy_send "宿主机 swap 偏高" "$now swap 已用 ${pct}% > ${SWAP_PCT}% (MemAvailable ${avail_mb}MB)"
    fi
  fi
}

note_event() {
  local now="$1" file="$STATE_DIR/events.ts"
  echo "$now" >> "$file"
  local cutoff=$(( now - WINDOW ))
  local tmp="$file.tmp"
  awk -v c="$cutoff" '$1+0 >= c' "$file" > "$tmp" && mv "$tmp" "$file"
  wc -l < "$file" | tr -d ' '
}

cmd_events() {
  echo "host_memwatch: 监听 docker events (container=$CONTAINER)" >&2
  local last_oom=0
  docker events --filter type=container --filter event=oom --filter event=die \
    --format '{{.Time}} {{.Action}} {{.Actor.Attributes.name}} {{.Actor.Attributes.exitCode}}' |
  while read -r ts action name exitcode; do
    [[ "$name" == "$CONTAINER" ]] || continue
    local now
    now="$(date +%s)"
    # 一次 OOM 会先后出 oom 与 die(137) 两条事件, 10s 内只推一次
    if [[ "$action" == "oom" || "$exitcode" == "137" ]]; then
      if (( now - last_oom > 10 )); then
        ntfy_send "stock-visual OOM" "$(date '+%F %T') 容器被杀掉 action=$action exit=${exitcode:-?}"
      fi
      last_oom="$now"
    fi
    # 只按 die 计数: 每次退出必有一条 die, 再数 oom 会把一次 OOM 算成两次
    [[ "$action" == "die" ]] || continue
    local n
    n="$(note_event "$now")"
    if (( n > RESTART_MAX )); then
      if cooled "restart-storm"; then
        ntfy_send "stock-visual 重启风暴" "$(date '+%F %T') ${WINDOW}s 内退出 ${n} 次 (阈值 ${RESTART_MAX})"
      fi
    fi
  done
}

case "${1:-}" in
  check) cmd_check ;;
  events) cmd_events ;;
  *)
    echo "用法: $0 check|events" >&2
    exit 2
    ;;
esac
