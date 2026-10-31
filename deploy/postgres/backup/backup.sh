#!/bin/bash
# LogHarvest 定时 pg_dump 归档：每轮 dump | gzip 落 BACKUP_DIR，按天回收过期归档。
# 环境变量（与 deploy/postgres/docker-compose.yml 的 backup 服务对应）：
#   POSTGRES_HOST / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB   连接目标
#   SCHEDULE=@daily     支持三种写法：@daily（每天 00:00 UTC）/ @hourly / "HH:MM"（每日定时，UTC）
#   BACKUP_KEEP_DAYS=14 归档保留天数；只回收本脚本产生的 logharvest-*.sql.gz，
#                       Jenkins 的 pre-deploy-* 与手工 manual-* 归档不受影响
#   BACKUP_DIR=/backups 归档目录（compose 挂载到 /usr1/logharvest/postgres/backup）
set -euo pipefail

DB_HOST="${POSTGRES_HOST:-postgres}"
DB_USER="${POSTGRES_USER:-logharvest}"
DB_NAME="${POSTGRES_DB:-logharvest}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
SCHEDULE="${SCHEDULE:-@daily}"

export PGPASSWORD="${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
mkdir -p "$BACKUP_DIR"

stop() { echo "backup: 收到停止信号，退出"; exit 0; }
trap stop TERM INT

next_run_seconds() { # 距下一次备份的秒数
    local h m s now tgt
    h="$(date +%H)"; m="$(date +%M)"; s="$(date +%S)"
    case "$SCHEDULE" in
        @hourly | hourly)
            echo $((3600 - 10#$m * 60 - 10#$s)) ;;
        "" | @daily | daily)
            echo $((86400 - 10#$h * 3600 - 10#$m * 60 - 10#$s)) ;;
        *:*)
            tgt=$((10#${SCHEDULE%%:*} * 3600 + 10#${SCHEDULE#*:} * 60))
            now=$((10#$h * 3600 + 10#$m * 60 + 10#$s))
            [ "$tgt" -le "$now" ] && tgt=$((tgt + 86400))
            echo $((tgt - now)) ;;
        *)
            echo "backup: 不支持的 SCHEDULE=$SCHEDULE（支持 @daily / @hourly / 每日 HH:MM）" >&2
            exit 2 ;;
    esac
}

prune_old() { # 只回收本脚本命名前缀的归档
    find "$BACKUP_DIR" -maxdepth 1 -name 'logharvest-*.sql.gz' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
}

echo "backup: host=$DB_HOST db=$DB_NAME schedule=$SCHEDULE keep=${KEEP_DAYS}d dir=$BACKUP_DIR"
while true; do
    STAMP="$(date +%Y%m%d-%H%M%S)"
    DEST="$BACKUP_DIR/logharvest-$STAMP.sql.gz"
    echo "backup: pg_dump $DB_NAME -> $DEST"
    # 先写 .part 再原子改名，避免容器中途被杀留下半截归档被当成可用备份
    if pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" | gzip > "$DEST.part"; then
        mv "$DEST.part" "$DEST"
        echo "backup: ok $(du -h "$DEST" | cut -f1)"
    else
        echo "backup: pg_dump 失败（下个周期重试）" >&2
        rm -f "$DEST.part"
    fi
    prune_old
    # sleep 放后台 + wait：SIGTERM 可打断等待立即退出（bash 对前台子进程会等它结束才跑 trap）
    sleep "$(next_run_seconds)" &
    wait $! || true
done
