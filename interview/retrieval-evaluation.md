# RAG 检索评测集怎么做

> 怎么从"5 条 query 肉眼判断"升级到"能拍板说改进有效"的评测体系。
>
> 配套阅读:
> - [multi-channel-rag-architecture.md](./multi-channel-rag-architecture.md) — 系统设计
> - [bm25-keyword-channel.md](./bm25-keyword-channel.md) — 单通道优化
>
> 本文回答:有了系统、改了一轮通道,**怎么严肃证明改进是真改进、不是错觉**。

---

## 一句话版本(电梯)

> 用 **pooling + LLM-as-judge + 人工 spot-check** 三步走,
> 30 query 起步、100 query 达到方向性可信,
> 算 **recall@10 / nDCG@10 / MRR** 三个指标做配置 A/B,
> 总成本一天到三天,远比"上 ES、加 reranker、瞎调参数"更早见到价值。

---

## 30 秒版本

我们的检索系统有 5 个通道,改一个通道、调一个阈值都没法预先判断好坏 —— 必须看真实 query 上的指标。但**完全人工标注成本太高**(100 query × 10 候选 × 2 个标注员 ≈ 一周)。

我用一个**三段式的低成本方案**:
1. **Pooling**:跑多个检索配置,合并候选 → 这就是要标注的 doc 池
2. **LLM-as-judge**:GPT-4o 给每个 (query, doc) pair 打 0-3 分,占 95% 的标注量
3. **人工 spot-check**:抽 10% 让人标,跟 LLM 比一致性,校准后才放心用

最终算 **recall@10、nDCG@10、MRR** 三个指标。改任何配置前后跑一遍这套,数据说话。

---

## 完整版本

### 1. 为什么要标注集 — 没标注集会怎样

回看 [bm25-keyword-channel.md](./bm25-keyword-channel.md) 的诚实评测段:
我证明了"LIKE 没在工作(5/5 全 0)",但**没证明 BM25 排序质量好**。

这是检索优化里最常见的盲区:

| 现象 | 真实判断 |
|---|---|
| 候选数变多了 | 召回涨了?还是只是阈值松了? |
| top-10 看起来更相关 | 主观还是客观?换组 query 还成立吗? |
| 上线后用户没反馈 | 是真的好了,还是用户根本不会区分? |

**没指标的优化是凭感觉**,只有标注集 → 指标 → A/B 的链路打通,才能说"这次改动让 recall@10 从 0.42 涨到 0.58"。

### 2. 标注集的本质

对检索任务,标注集就是一个表:

| query_id | query | doc_id | relevance |
|---|---|---|---|
| q001 | "GPT-4o 多模态能力" | art_4421 | 3 (强相关) |
| q001 | "GPT-4o 多模态能力" | art_4422 | 0 (无关) |
| q001 | "GPT-4o 多模态能力" | art_4423 | 2 (相关) |
| ... | | | |

两种风格:

- **Binary**:relevance ∈ {0, 1},简单但损失"部分相关"信号
- **Graded**:relevance ∈ {0, 1, 2, 3},TREC 标准,nDCG 算得准

新闻聚合场景推荐 **graded**,因为有大量"沾边但不切题"的文章,binary 会浪费这个区分度。

### 3. 三种标注路线对比

| 路线 | 准确度 | 成本 | 可扩展性 | 何时该选 |
|---|---|---|---|---|
| 全人工 | 最高 | $$$$ | 差 | < 50 query 的发表级评测 |
| Pooling + 人工 | 高 | $$ | 中 | 学术 IR 标配(TREC) |
| LLM-as-judge | 中-高 | $ | 极好 | 工业界、快速迭代 |
| 纯线上点击 | 真实但有 bias | $ | 极好 | 已上线、有用户 |

**当前阶段(项目早期 + 单人开发)推荐 Pooling + LLM-as-judge**,
两者结合是工业界主流(MTEB / BEIR 后期都是这种思路)。

### 4. 完整步骤(可落地版)

#### Step 1: 构造 query 集(~1 小时)

**好的 query 集要覆盖你的真实分布**。三个来源,推荐加权混合:

1. **真实用户 query**(如果有日志)—— 最高优先级
2. **从文章标题反向生成** —— 用 LLM 给每篇热门文章生成 1-2 个"用户可能用什么 query 找到它"
3. **手工设计典型类型** —— 字面量型、时效型、知识型、实体型、模糊型 各占 ~20%

数量:
- **30 query**:directional,半天能搞定,够拍板小改动
- **100 query**:方向性可信,可以发内部 report
- **500+**:industrial benchmark,对外 publish 的体量

```python
# scripts/build_query_set.py
queries = [
    # 字面量型(检验 BM25)
    {"id": "q001", "query": "GPT-4o", "type": "literal"},
    {"id": "q002", "query": "Kubernetes operator pattern", "type": "literal"},
    # 时效型(检验 social + external)
    {"id": "q010", "query": "AI startup funding April 2026", "type": "temporal"},
    # 知识型(检验向量通道)
    {"id": "q020", "query": "transformer attention mechanism", "type": "conceptual"},
    # 实体型(检验综合)
    {"id": "q030", "query": "Sam Altman Sora launch", "type": "entity"},
    # 模糊型(检验鲁棒性)
    {"id": "q040", "query": "那个新出的 AI 模型 deepsek", "type": "fuzzy"},
    # ...至少每类 5-10 条
]
```

#### Step 2: Pool candidates(~30 分钟)

对每条 query,跑**多种检索配置**,合并所有出现过的候选 doc。这是 pooling 的核心 idea —— **不可能标注全库**,但所有可能进 top-K 的文档都在 pool 里。

```python
configs = {
    "current": rag_search,              # 当前生产配置
    "no_keyword": rag_search_no_kw,     # 关掉 BM25
    "no_external": rag_search_no_ext,   # 关掉 external
    "high_threshold": rag_search_strict # 阈值更严
}

pool = {}  # query_id → set of doc_ids
for q in queries:
    pool[q["id"]] = set()
    for cfg_name, search_fn in configs.items():
        results = await search_fn(q["query"], top_k=20)
        for chunk in results:
            pool[q["id"]].add(chunk.id)
```

典型规模:30 query × ~30-50 unique docs/query = ~1000-1500 (q, d) pairs。
一个 LLM 标完成本约 $5-10。

#### Step 3: LLM-as-judge 自动标注(~1-2 小时)

用 GPT-4o / Claude 给每个 (query, doc) pair 打分。
**关键是 prompt 设计**:

```python
JUDGE_PROMPT = """You are an expert IR evaluator. Rate the relevance
of the document to the query on a 4-point scale:

3 = Highly relevant: the document directly answers or is centrally
    about the query topic
2 = Relevant: the document discusses the query topic substantively
    among other things
1 = Marginally relevant: the document mentions the query topic but
    it's not a focus
0 = Not relevant: the document does not address the query topic

Query: {query}
Document title: {title}
Document content (first 800 chars): {content}

Output ONLY a single integer 0/1/2/3, no explanation.
"""

async def judge(query, doc):
    resp = await openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(...)}],
        temperature=0,
    )
    return int(resp.choices[0].message.content.strip())
```

**关键技巧**:
- `temperature=0` 保证可复现
- 只输出数字,不让它解释 → 省 token、避免 reasoning bias
- 同一 pair 跑 3 次取众数,稳定性更高(成本 3x,可选)
- 用 `gpt-4o-mini` 而非 `gpt-4o`,成本降 10x,质量损失约 5%

#### Step 4: 人工 spot-check 校准(~1 小时)

**不能盲信 LLM**,抽 10% 样本人工标:

```python
import random
sample = random.sample(all_pairs, k=int(0.1 * len(all_pairs)))
# 把 sample 导出成 CSV,人手标注
# 算 LLM 和人的一致性
```

衡量一致性:
- **Cohen's Kappa**:>0.6 是 substantial agreement,可以用
- **Pearson correlation**:相关性 >0.7 也行
- **完全一致率**:>70% 直观但严格

如果一致性差,**调 prompt 重跑**(常见原因:scale 定义模糊、文档截断太短)。

#### Step 5: 算指标(~10 分钟代码)

三个核心指标:

```python
def recall_at_k(retrieved_ids, relevant_ids, k=10):
    """前 k 个结果里有多少个是 relevant 的 / 总 relevant 数"""
    top_k = retrieved_ids[:k]
    hit = len(set(top_k) & relevant_ids)
    return hit / len(relevant_ids) if relevant_ids else 0.0

def ndcg_at_k(retrieved_ids, relevance_map, k=10):
    """Normalized Discounted Cumulative Gain"""
    import math
    dcg = sum(
        relevance_map.get(doc_id, 0) / math.log2(i + 2)
        for i, doc_id in enumerate(retrieved_ids[:k])
    )
    ideal_relevances = sorted(relevance_map.values(), reverse=True)[:k]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal_relevances))
    return dcg / idcg if idcg > 0 else 0.0

def mrr(retrieved_ids, relevant_ids):
    """Mean Reciprocal Rank — 第一个 relevant 的位置倒数"""
    for i, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / i
    return 0.0
```

| 指标 | 答什么问题 |
|---|---|
| Recall@10 | 前 10 个里召回了几个相关的(覆盖率) |
| nDCG@10 | 既看召回又看排序质量,排得越靠前越好(综合质量) |
| MRR | 用户最关心的"第一个相关结果排第几"(用户体验) |

报告时**三个都报**,各自反映不同侧面。

#### Step 6: A/B 测试配置(每次改完就跑)

```python
configs_to_compare = {
    "v0_baseline": {"keyword": "like", "rerank": False},
    "v1_bm25":     {"keyword": "bm25", "rerank": False},
    "v2_bm25_rr":  {"keyword": "bm25", "rerank": True},
}

results = {}
for name, cfg in configs_to_compare.items():
    metrics = []
    for q in queries:
        retrieved = run_with_config(cfg, q["query"], top_k=10)
        m = {
            "recall@10": recall_at_k(retrieved, relevant[q["id"]]),
            "ndcg@10": ndcg_at_k(retrieved, relevance_map[q["id"]]),
            "mrr": mrr(retrieved, relevant[q["id"]]),
        }
        metrics.append(m)
    # 平均 + 95% bootstrap CI
    results[name] = {
        k: (mean([m[k] for m in metrics]),
            bootstrap_ci([m[k] for m in metrics]))
        for k in ["recall@10", "ndcg@10", "mrr"]
    }
```

报告样例:

```
Config            recall@10           nDCG@10           MRR
v0_baseline       0.42 ± 0.06         0.31 ± 0.05       0.58 ± 0.08
v1_bm25           0.51 ± 0.05  ↑      0.36 ± 0.04  ↑    0.61 ± 0.07
v2_bm25_rr        0.51 ± 0.05         0.48 ± 0.04  ★    0.74 ± 0.06  ★

★ = 95% CI 不重叠,统计意义显著
```

**这才是"我让 nDCG@10 涨了 17 个百分点"的可信叙事。**

### 5. 几个常见 pitfall

#### Pitfall 1: Pooling bias

只标注 pool 里的 docs,意味着**没被任何配置召回过的 doc 永远是 0 分**。
如果未来某个新算法能召回到 pool 外的好 doc,标注集会**低估**它的真实分。

**对策**:
- 把 pool 做大(多种配置 + 高 top-K)
- 定期重新 pool
- 接受这个偏差,作为已知 bug

#### Pitfall 2: LLM judge bias

LLM 有自己的"相关性偏好",可能跟用户判断不一致。常见偏向:
- 偏好长文档 / 信息密集的文档
- 对同语种 query+doc 评分偏高
- 对包含 query 字面量的文档评分偏高(BM25 友好,但不一定真相关)

**对策**:
- 人工 spot-check 校准
- 用多个不同 LLM(GPT-4o + Claude)取平均,降低单模型 bias
- 跟最终用户行为(点击 / 停留时间)对照,不要只信 LLM

#### Pitfall 3: 评测集和训练集泄漏

如果你之后要训 reranker 或 LTR,**确保评测 query 不在训练集里**。
听起来废话但容易忘。

#### Pitfall 4: query 分布偏移

你 30 条 query 全是英文 tech,但真实用户 30% 是中文 / 30% 是非 tech 话题 →
评测分高不代表生产好。

**对策**:从生产日志按比例采样,或至少**记录评测集的覆盖类别分布**。

#### Pitfall 5: 数据陈旧

新闻聚合场景里,3 个月前 query "Sam Altman recent" 的相关 doc,
今天可能完全不相关了(时效信号变了)。

**对策**:
- 时效型 query 标注集要按时间段维护
- 标注时记录"标注日期",评测时只比对 ±1 周内的 doc

### 6. 实操路线图(给本项目的具体建议)

按时间投入排:

| 时长 | 该做什么 | 给你什么 |
|---|---|---|
| 半天 | 30 query + LLM judge,无人工 | 内部小迭代时拍板用,不能对外 |
| 2 天 | + 100 query + 10% 人工校准 | 可以写在简历 / 内部 report |
| 1 周 | + 多 LLM judge + 时效 query | 工业级标准 |
| 2-4 周 | + 真用户日志 + click data | 可发表 / 对外 benchmark |

**当前阶段建议先做半天版**:
1. 复用 [`probe_channels.py`](../agent/scripts/probe_channels.py) 的 5 query,扩到 30
2. 跑 2-3 种配置 pool 一下(关 keyword / 关 external / 默认)
3. GPT-4o-mini 标注(cost ~$1)
4. 算 recall@10 + nDCG@10
5. 跑一次 LIKE vs BM25 对比,**生成第一版可信报告**

总投入半天,拿到的东西 **直接能进简历项目部分**。

### 7. 推荐的工具 / 框架

不需要从零造,几个推荐:

- **`pytrec_eval`**:Python 包,直接吃 TREC qrels 格式,算所有标准 IR 指标
- **`ranx`**:更现代的 Python IR 评测库,有 RRF / 融合算法 baseline
- **`pyterrier`**:学术界用得多,带 BM25 / DPR 等 baseline 检索器
- **Label Studio**:开源标注 UI,人工标 spot-check 时用
- **Argilla**:专注 LLM-as-judge 的标注平台,集成 OpenAI/Anthropic API
- **MTEB / BEIR**:公开 IR 评测基准,可以借里面的 query 灵感

不推荐:自己造 UI、自己实现 nDCG。**评测代码出 bug 比检索代码出 bug 更难发现**,
用成熟库。

---

## 进阶追问(技术深度题)

### Q1: 为什么 nDCG 用 log2 不用其他?

A:`gain / log2(rank+1)` 是 1995 年原论文的设计。
log2 的直觉是 **"从第 1 名到第 2 名的边际损失,远大于从第 100 名到第 101 名"**,
这跟用户行为一致(用户主要看前几条)。

换 log10 / log_e 都行,只是 scale 不同,**relative ranking 不变**。
log2 是约定俗成。

### Q2: Recall 和 Precision 不是更直观吗?为什么用 nDCG?

A:三个理由:

1. **Recall@K 不看排序** —— 相关 doc 排第 1 还是第 10,recall@10 是一样的。但用户体验差很多
2. **Precision@K 把 graded relevance 拍成 binary** —— 损失了"部分相关"的信号
3. **nDCG 同时考虑召回 + 排序 + 分级**,是 IR 公认的"最像人类感觉"的单一指标

工业界常报 nDCG@10 / nDCG@5 作为综合指标,recall 和 precision 作为细粒度补充。

### Q3: LLM-as-judge 不是有 bias 吗?为什么还能用?

A:**有 bias 不等于不能用**,关键看 bias 是不是 systematic 且可测量:

- **Random noise** → 多跑几次取平均消解
- **Systematic bias**(比如总偏好长文)→ 人工 spot-check 能发现,然后改 prompt
- **Position bias / order bias** → 已知问题,可以 shuffle pair 顺序消解

行业里 GPT-4 as judge 跟人类标注的 Cohen's Kappa 一般在 0.5-0.7 之间,
**比两个不同人之间的标注一致性还高**(人类 inter-annotator agreement 也就 0.4-0.6)。
不是说 LLM 完美,是说**人也未必更可信**。

### Q4: 30 query 够吗?统计上显著性怎么办?

A:对单一指标(比如 recall@10),30 个样本算均值的 95% CI 大约是 ±0.05-0.10。
意思是:

- 配置 A recall@10 = 0.42
- 配置 B recall@10 = 0.51
- 差 0.09,**接近但可能不显著**

要更确信,有两条路:
1. **加 query 到 100 条**,CI 收缩到 ±0.03
2. **Bootstrap resampling**:从 30 query 里有放回采样 1000 次,看每次哪个配置赢,如果 95% 次数 A 输给 B,说服力够

写代码:
```python
import numpy as np
def bootstrap_winrate(scores_a, scores_b, n=10000):
    wins = 0
    for _ in range(n):
        idx = np.random.choice(len(scores_a), len(scores_a), replace=True)
        if np.mean(scores_b[idx]) > np.mean(scores_a[idx]):
            wins += 1
    return wins / n
```

### Q5: 怎么判断标注质量好坏?

A:三个维度:

1. **Inter-annotator agreement**(人 vs 人 / 人 vs LLM)→ Kappa
2. **Test-retest reliability**(同一标注员隔几天再标一次)→ 一致性
3. **Face validity**(随机抽 10 条,讲故事看合不合理)

任一指标差,就要回头改:
- Kappa 低 → scale 定义太模糊,加例子到 prompt
- Test-retest 差 → 标注员太累 / 任务太长,分批
- Face validity 差 → 整个 pipeline 有 bug,先 debug

### Q6: 在线 A/B 测试 vs 离线评测,关系是什么?

A:**互补不替代**:

| | 离线评测 | 在线 A/B |
|---|---|---|
| 速度 | 快(分钟级) | 慢(几天到几周收数据) |
| 成本 | 低(标注 + 计算) | 高(暴露给用户、可能影响业务) |
| 信号 | 间接(标注 vs 真实满意度) | 直接(点击、转化) |
| 适合 | 早期迭代、过滤明显失败 | 最终决策、长期效果 |

工业界标准玩法:**离线把候选方案过滤到 2-3 个,在线 A/B 选最终赢家**。
没有离线评测,在线 A/B 会被淹没在烂方案里;没有在线 A/B,
离线赢的方案可能 over-fit 标注集。

### Q7: 没有用户点击数据,怎么开始?

A:**没有就先用 LLM 当代理**,有了再迁移:

阶段 1(现在):LLM-as-judge 标 → 离线评测
阶段 2(上线后 2-3 个月):收集 click-through、停留时间、播放完成率 → 用户行为数据
阶段 3(数据足够):**直接用用户行为训练 LTR model**,LLM judge 退役为质检员

不要等"有完美数据"才开始评测,**用 LLM 起步成本极低,先跑起来再优化**。

### Q8: 标注集会不会过时?多久更新一次?

A:看你的内容更新频率:

- **静态语料(论文 / 技术文档)**:几年都不用更新
- **新闻聚合(本项目)**:**3-6 个月更新一次**,因为时效型 query 的相关 doc 完全变了
- **电商搜索**:每月更新,因为商品 SKU 频繁变

**对策**:把"标注日期"作为字段,评测时只对比相同时间窗内的 doc,
跨窗口的 query 重新评估。

---

## 简历上怎么写

短版:
> 设计 RAG 检索评测体系:30+ query 标注集 + LLM-as-judge + 人工校准,
> 用 recall@10 / nDCG@10 / MRR 量化每次架构改动的收益。

详版:
> 为多路 RAG 检索系统设计离线评测体系。采用 pooling 策略合并多配置候选,
> 结合 GPT-4o-mini 自动标注与人工 spot-check 校准 (Cohen's Kappa 验证),
> 总投入 1 天构建 100 query 评测集。计算 recall@10 / nDCG@10 / MRR 并报告
> bootstrap 95% CI,使每次 retrieval 改动(BM25 替换 / 阈值调整 / reranker 接入)
> 都有可信的量化结论。

---

## 关键文件 / 落地路径

如果决定动手做,建议新增:

```
agent/eval/                          ← 新增评测模块
├── __init__.py
├── queries.py                       ← 评测 query 集
├── pooler.py                        ← 多配置 pool 候选
├── judge.py                         ← LLM-as-judge 标注
├── metrics.py                       ← recall / ndcg / mrr 计算
└── runner.py                        ← 一键跑 A/B 报告

scripts/
├── probe_channels.py                (现有,保留作为快速诊断)
└── run_eval.py                      ← 新增,跑完整评测
```

参考实现量:300-500 行,2-3 天能从无到一版可用。
