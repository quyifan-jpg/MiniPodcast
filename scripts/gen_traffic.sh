#!/usr/bin/env bash
# 模拟真实流量：混合接口 + 随机参数 + 间歇性峰值
# 用法：./gen_traffic.sh [duration_seconds]
# 默认跑 600 秒（10 分钟），生成的指标足够 Grafana 画出有意义的曲线
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
DURATION="${1:-600}"
END=$(( $(date +%s) + DURATION ))

echo "Generating traffic to $BASE for ${DURATION}s..."

burst() {
    # 短时间高并发，模拟流量峰值（让你的 P99 / HPA 有故事可讲）
    local n=$1
    for _ in $(seq 1 "$n"); do
        page=$(( (RANDOM % 20) + 1 ))
        curl -s -o /dev/null "$BASE/api/articles/?page=$page&per_page=10" &
    done
    wait
}

while [ "$(date +%s)" -lt "$END" ]; do
    # 70% 列表、20% 详情、5% 错误请求（让 error 指标有数据）、5% 峰值
    r=$(( RANDOM % 100 ))
    if [ "$r" -lt 70 ]; then
        page=$(( (RANDOM % 20) + 1 ))
        per=$(( (RANDOM % 30) + 5 ))
        curl -s -o /dev/null "$BASE/api/articles/?page=$page&per_page=$per" &
    elif [ "$r" -lt 90 ]; then
        id=$(( (RANDOM % 1000) + 1 ))
        curl -s -o /dev/null "$BASE/api/articles/$id" &
    elif [ "$r" -lt 95 ]; then
        # 故意打 404 / 500，让你能在指标里看到 client_error / error 的差别
        curl -s -o /dev/null "$BASE/api/articles/-1"
    else
        echo "[burst] $(date +%H:%M:%S)"
        burst 20
    fi
    # 控制基础速率：每 100ms 一个新请求 → 平均 ~10 QPS
    sleep 0.1
done

wait
echo "Done."
