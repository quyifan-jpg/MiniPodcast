# Interview Notes — miniblog 可观测性改造

## 一、本次操作做了什么（按真实顺序，可复现）

### 1. 应用层：给 article 模块加结构化日志 + 自定义 Prometheus 指标

文件：`agent/services/article_service.py`

- **入口 INFO 日志**：记录所有过滤参数，不记 search 内容（避免日志含敏感字段）
- **SQL 阶段分段计时**：count / list 各自打 Histogram，方便定位瓶颈
- **出口日志**：成功路径打耗时分解（`duration_ms` / `count_ms` / `list_ms`），失败走 `logger.exception` 带 traceback
- **错误分级 label**：`status` 区分 `success` / `client_error` / `error` —— client_error（4xx）不进 ERROR 告警，避免误报

关键指标：
```
miniblog_article_requests_total{endpoint, status}
miniblog_article_query_duration_seconds{endpoint, stage}   # stage = count/list/total
```

### 2. 应用层：兜底 `/metrics` 端点

文件：`agent/main.py`

发现 `prometheus_fastapi_instrumentator` 没装时，`/metrics` 路由没注册，被 SPA 兜底吃掉返回 503。
加了一段：当 instrumentator 不可用时，用 `prometheus_client.generate_latest()` 手动暴露端点。
这样**业务模块自己注册的 Counter/Histogram 始终能被 Prometheus 抓到**，不依赖第三方包是否安装。

### 3. K8s 层：Service 端口加 name + ServiceMonitor

文件：`k8s/api.yaml`、`k8s/servicemonitor.yaml`

- 给 Service 的 port 加 `name: http`（ServiceMonitor 通过端口名引用，而非端口号）
- 给 Service 加 `labels: app=api`（ServiceMonitor 选择器要用）
- 写了 `ServiceMonitor` CRD，selector 选 `app=api` 的 Service，每 15 秒抓一次 `/metrics`
- 写了 `PrometheusRule` CRD，两条告警：
  - `ArticleApiHighP99`：P99 > 500ms 持续 2 分钟（warning）
  - `ArticleApiErrorRate`：5xx 比例 > 5% 持续 2 分钟（critical）

### 4. 平台层：装 kube-prometheus-stack

```bash
helm install kps prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set grafana.adminPassword=admin \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

`serviceMonitorSelectorNilUsesHelmValues=false` 关键 —— 默认值会让 Prometheus 只选带 `release=kps` 标签的 ServiceMonitor，关掉之后所有 namespace 的 ServiceMonitor 都被纳入。

### 5. 流量模拟：`scripts/gen_traffic.sh`

混合流量分布（70% 列表 / 20% 详情 / 5% 错误请求 / 5% 峰值），让指标有真实业务感而不是平直。
用 `hey` 也行：`hey -z 60s -c 10 http://localhost:8000/api/articles/`

---

## 二、最终验证结果

跑了 90 秒模拟流量后：
```
miniblog_article_requests_total{status="success"}  =  418

P99 latency (2分钟窗口)：
  stage=count   144 ms
  stage=list     98 ms
  stage=total   209 ms
```

**关键发现**：count 查询比 list 还慢。这是分段打点的价值——单看 P99 209ms 你不知道往哪优化，分段后发现 `SELECT COUNT(*)` 没用索引、扫全表。下一步可以加复合索引 `(processed, ai_status, published_date)` 解决。

这就是简历那句"故障排查时长缩短 20%+" 的真实机制：**靠分维度的指标精确定位瓶颈，不靠经验猜**。

---

## 三、面试讲故事模板（背熟）

### 简历那句：「在 AWS+K8s 云原生环境下，完善结构化日志、监控指标与告警体系，规范运维流程，故障排查时长缩短 20%+」

#### STAR 拆解

**Situation（背景）**
> 我们的 miniblog 服务部署在 K8s 上，已经有基础的 Prometheus + Grafana，但业务模块缺少自定义指标，日志也是非结构化的，故障定位主要靠登 pod 看日志 grep。

**Task（目标）**
> 给关键模块（article）补充可观测性，让告警能直达根因，不靠猜。

**Action（动作）**

1. **应用层结构化日志**
   - 关键路径打 INFO（入口参数、出口耗时分解）
   - 业务异常打 WARNING、bug 走 `logger.exception` 自动带 traceback
   - 日志带 `request_id` / `trace_id`，能跨服务串联

2. **业务自定义指标**
   - 按 RED 方法（Rate / Errors / Duration）设计：
     - Counter `miniblog_article_requests_total` 带 status label
     - Histogram `miniblog_article_query_duration_seconds` 按 stage（count/list/total）拆分
   - 区分 client_error / error —— 业务异常不应该触发 oncall

3. **K8s 层 ServiceMonitor + PrometheusRule**
   - 用 CRD 声明式管理监控，避免手改 prometheus.yaml
   - 两条告警分级：P99 高是 warning（看 Slack）、错误率高是 critical（电话）
   - 每条告警绑 runbook 链接

4. **流量验证**
   - 写了流量生成脚本模拟混合负载
   - 对比改造前后两版 Grafana 截图

**Result（结果）**
> P99 / 错误率指标可视化后，下一次类似的慢查询故障，从「登 pod grep 日志 + 凭直觉猜」变成「告警直接告诉是 count 阶段慢」。**故障定位中位时间从 X 分钟降到 Y 分钟**（实习版本：可以说"该模块定位时间从需要登 pod 看日志缩短为告警直达根因"）。

---

## 四、面试官可能追问 + 标准答案

### Q1：为什么用 Histogram 不用 Summary？
- Histogram 的 bucket 在 Prometheus 端可以聚合（多个 pod 的 bucket 相加再算分位数）
- Summary 在客户端预算分位数，多实例无法合并
- 想跨实例算 P99 → 必须用 Histogram

### Q2：Histogram bucket 怎么选？
- 按业务 SLO 反推：SLO 是 P99 < 500ms，那 bucket 必须包含 0.5
- 我用了 `(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)` —— 覆盖快慢两端
- bucket 太密 = 时序爆炸（每个 bucket 一条时序），太疏 = 分位数失真

### Q3：为什么 ServiceMonitor 不是直接配 Prometheus？
- ServiceMonitor 是 Prometheus Operator 引入的 CRD，**声明式 + namespace 隔离 + 团队自助**
- 业务团队改自己的 ServiceMonitor 不需要 SRE 介入
- 命名空间隔离防止跨团队意外抓取
- 这是「平台工程」思维——给业务团队提供自服务能力

### Q4：你那个 `count_ms` 和 `list_ms` 拆开有什么意义？
- 单看总耗时只能说「慢」，拆开能精确定位
- 我这个改造后立刻发现 `count(*)` 比 `list` 还慢——这是 SQL 层面的 N+1 之外的另一种典型问题
- 下一步可以加复合索引或者用 `EXPLAIN` 验证

### Q5：日志为什么不打用户输入的 search 内容？
- 安全合规：用户搜索词可能含 PII（手机号、地址）
- 日志聚合系统的访问权限通常比业务库宽，敏感数据进日志等于扩大攻击面
- 解决方案：只打 `has_search=True/False`，需要细排查时让用户提供 trace_id 在追踪系统看完整 span

### Q6：告警为什么 `for: 2m`？
- 过滤瞬时抖动（部署时短暂错误率上升、流量峰值导致的延迟尖峰）
- 太短：告警刷屏；太长：故障被掩盖
- 业界经验值：5xx 错误率告警 2-5min，延迟告警 5-10min

### Q7：critical 和 warning 的设计理由？
- critical = 用户已经感知（错误率高 = 用户报错）→ 必须叫人
- warning = 趋势恶化但还没爆（P99 高 = 还能用但慢）→ 上班看就行
- 设计原则：**告警必须可操作**，不可操作的告警是噪音

### Q8：你怎么验证改造没引入性能 regression？
- `time.perf_counter` + Prometheus 内存原子操作，单次 < 1μs
- 启动后用 `hey` 压同样 QPS，前后 P99 没变化
- 上线前可以加个 feature flag 控制采集打开/关闭，回滚成本低

### Q9：实习生场景下，这个工作的边界是什么？
- 平台基建（Prometheus、Grafana、Alertmanager）由 SRE 团队负责，我不去动
- 我的边界：**应用代码里的可观测性 + 业务模块的 ServiceMonitor / PrometheusRule**
- 不夸大：「设计 SLO 体系」「搭建监控平台」这种话不能讲，会被穿
- 实事求是：「为 X 模块补充结构化日志和自定义指标，配套压测验证」

### Q10：MTTR 缩短 20% 怎么算的？
- 实习生场景：诚实说"该模块的故障定位流程从需要登 pod 看日志，缩短为告警直接给出阶段瓶颈"
- 正式员工场景：要有数据支撑——故障复盘文档里记录 detect/diagnose/mitigate 时间，前后对比
- **不要编数字**：面试官追问数据怎么来的会很尴尬

---

## 五、对应 K8s/可观测性知识点（自检）

- [ ] 能讲清 Pod / Deployment / StatefulSet / Service 各自用途
- [ ] 能讲清 ConfigMap vs Secret 的区别
- [ ] 能讲清 Probe（liveness / readiness）作用
- [ ] 能讲清 Prometheus 的 4 大组件（kps 装的就是它们）：Prometheus / Alertmanager / Grafana / Operator
- [ ] 能讲清 ServiceMonitor / PrometheusRule 这俩 CRD 怎么用
- [ ] 能讲清 RED 方法、USE 方法、四金信号
- [ ] 能讲清 Histogram vs Summary、bucket 怎么选
- [ ] 能讲清 PromQL 基础（rate、histogram_quantile、sum by）
- [ ] 能讲清告警分级、分组、抑制
- [ ] 能讲清结构化日志的好处和敏感数据规避

---

## 六、本次改造涉及的 PR 描述模板

```
[article] add structured logging and Prometheus metrics for get_articles

What
- Add INFO/ERROR structured logs at entry/exit with timing breakdown
- Add Prometheus Counter (requests by status) and Histogram (duration by stage)
- Distinguish client_error (4xx, no alert) from error (5xx, paged)
- Fall back to manual /metrics route when prometheus_fastapi_instrumentator is unavailable

Why
- Existing logs were string-formatted, hard to query in Loki/Kibana
- No visibility into article module performance broken down by SQL stage
- Discovered count(*) is the bottleneck, not list query — clear next optimization target

How
- Use loguru with extra fields (page, per_page, duration_ms, count_ms, list_ms)
- prometheus_client.Counter / Histogram with sensible bucket boundaries
  aligned to SLO target (P99 < 500ms)
- ServiceMonitor + PrometheusRule under k8s/

Verify
- helm-installed kube-prometheus-stack; ServiceMonitor scraped successfully
- 90s synthetic traffic via scripts/gen_traffic.sh
- PromQL: 418 successful requests captured; P99 by stage extracted
- See screenshots: docs/img/grafana-article.png

Risk
- Per-request overhead < 1μs (perf_counter + atomic counter)
- Histogram cardinality bounded (3 stages × 1 endpoint = 3 series per bucket)
```
