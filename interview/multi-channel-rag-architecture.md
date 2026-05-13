# 多路检索引擎 + RRF 融合架构

> 给"在 RAG 项目里设计了多路并行检索 + RRF 融合"找一个能展开聊
> 30 分钟也不露馅的讲法。
>
> 配套文档:[bm25-keyword-channel.md](./bm25-keyword-channel.md)
> 是这套架构里**一个通道**的优化故事;本文是**整套架构**的设计与权衡。

---

## 一句话版本(电梯)

> 在播客生成项目里设计实现了一套 multi-channel retrieval engine:
> 5 个检索通道(2 个向量、1 个 BM25、1 个 social、1 个 external)并行执行,
> 4 个 post-processor(dedup → RRF → rerank → quality filter)串行融合,
> 单通道故障隔离,RRF 跨通道公平排序,整套是 sub-10s 的 RAG 召回链路,
> 用 Python asyncio + dataclass 实现,~700 行核心代码无外部依赖。

---

## 30 秒版本(自我介绍式)

我们的播客脚本生成需要给 LLM 提供高质量、多视角的素材。
我设计了一套**两阶段**的多路检索引擎:

- **Phase 1**:5 个通道并行跑(`asyncio.gather`),每条通道独立选数据源、独立打分、独立超时,**单点故障不影响其他通道**
- **Phase 2**:4 个 post-processor 链式跑(dedup → RRF → rerank → quality filter),把 30~60 个候选过滤排序成最终 top-10

核心设计是 **RRF(Reciprocal Rank Fusion)** —— 因为不同通道的 score
量纲完全不同(cosine vs BM25 vs engagement count),
RRF 只看每个通道里的 rank 不看 raw score,做到跨通道公平融合。

整个引擎是**严格无状态**的,所有上下文都通过 `SearchContext` dataclass
流转,通道和 post-processor 都是 plug-in,加新通道改 3 行代码。

---

## 完整版本(技术深聊)

### 1. 设计目标:为什么是多路而不是单路

播客选题这种场景对召回有几个特殊要求:

| 需求 | 单一通道为什么不够 |
|---|---|
| 既要语义近似,又要精确字面量 | Dense vector 把 "GPT-4o" 和 "GPT-5" 看得很近 |
| 既要库内深度,又要库外时效 | FAISS 里只有抓过的文章,刚发的新闻还没入库 |
| 既要长篇文章,又要短社交反应 | 文章和 tweet 风格完全不同,一个模型很难都好 |
| 任一数据源故障不能拖垮整体 | OpenAI 503 / DDG 超时是常态 |

**单路 + reranker** 解决不了 1 和 2(召回阶段就丢了);
**单路 + query rewriting** 解决不了 3 和 4(底层数据源还是单一)。
所以选了多路并行 + 后置融合。

### 2. 架构总览

```
                       ┌──────────────────┐
SearchContext  ──────► │   RetrievalEngine │
(query, top_k)         └─────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │   Phase 1: parallel channels        │
              │  (asyncio.gather, per-ch timeout)   │
              └─────────────────────────────────────┘
                                 │
   ┌──────────┬──────────┬───────┼───────┬──────────┬─────────┐
   │ Chunk    │ Article  │ Keyword│ Social│ External │  ...    │
   │ Vector   │ Vector   │ (BM25) │ Media │ (G + DDG)│         │
   │ FAISS    │ FAISS    │ memory │ MySQL │ HTTP     │         │
   │ pri=1    │ pri=2    │ pri=3  │ pri=5 │ pri=10   │         │
   └──────────┴──────────┴────────┴───────┴──────────┴─────────┘
                                 │
                       merge into List[Chunk]
                                 │
              ┌──────────────────┼──────────────────┐
              │   Phase 2: sequential post-process   │
              │  (chain of responsibility)           │
              └─────────────────────────────────────┘
                                 │
   ┌──────────────┬──────────────┬──────────────┬────────────┐
   │ Dedup        │ RRF          │ Rerank       │ Quality    │
   │ order=1      │ order=5      │ order=10     │ Filter     │
   │ priority-    │ Reciprocal   │ Cross-       │ order=20   │
   │ aware        │ Rank Fusion  │ encoder API  │ score+len  │
   └──────────────┴──────────────┴──────────────┴────────────┘
                                 │
                          final top-K chunks
```

#### 文件布局

```
agent/rag/
├── engine.py            ← MultiChannelRetrievalEngine (orchestrator)
├── factory.py           ← DI: 装配 channels + processors
├── models.py            ← SearchContext / RetrievedChunk / ChannelResult
├── config.py            ← rag_settings(toggles + 阈值)
├── bridge.py            ← 老 LangGraph pipeline 的兼容入口
├── keyword_index.py     ← BM25 内存索引(被 KeywordChannel 用)
├── channels/            ← 5 个通道实现
│   ├── base.py              ← SearchChannel 接口
│   ├── chunk_vector_channel.py
│   ├── article_vector_channel.py
│   ├── keyword_channel.py   (BM25)
│   ├── social_media_channel.py
│   └── external_search_channel.py
└── postprocessors/      ← 4 个后处理器
    ├── base.py              ← PostProcessor 接口
    ├── dedup.py
    ├── rrf.py
    ├── rerank.py
    └── quality_filter.py
```

### 3. 5 个通道:各自干什么、怎么互补

| Channel | priority | 数据源 | 强项 | 弱项 |
|---|---|---|---|---|
| ChunkVector | 1 | FAISS chunk index | 段落级语义召回(主力) | 字面量、跨实体推理 |
| ArticleVector | 2 | FAISS article index | 文章级宽召回 | 长文摘要被压缩信息丢失 |
| Keyword (BM25) | 3 | MySQL + 内存 BM25 | 字面量、缩写、版本号 | 同义词、跨语言 |
| SocialMedia | 5 | MySQL `social_media` | 实时反应、热度 | 短文本噪声大 |
| External | 10 | Google News + DDG | 库外兜底 | 慢(7-8s)、需 scrape |

**priority** 不影响并行执行,只在 **dedup 时决定保留哪个通道版本**——
priority=1 的 chunk 跟 priority=10 的 external 出现同一篇文章,保留 chunk
的版本(它已经是被处理好的段落,external 只有 URL 还要 scrape)。

### 4. 关键设计模式

#### Strategy Pattern: SearchChannel 接口

```python
class SearchChannel(Protocol):
    def channel_type(self) -> ChannelType: ...
    def priority(self) -> int: ...
    def is_enabled(self, ctx: SearchContext) -> bool: ...
    async def search(self, ctx: SearchContext) -> ChannelResult: ...
```

加新通道(比如 `StoryClusterChannel`)只要实现这 4 个方法,
然后在 `factory._build_channels()` 里加一行注册 —— **零侵入**。

#### Chain of Responsibility: PostProcessor

```python
class PostProcessor(Protocol):
    def order(self) -> int: ...     # 排序键,越小越早
    def is_enabled(self, ctx) -> bool: ...
    def process(self, chunks, all_results, ctx) -> list[chunks]: ...
```

每个 processor 接收上一步的 chunks,返回新 chunks。
**顺序通过 `order()` 排序而不是硬编码**,
所以 dedup 一定先于 RRF 跑、quality filter 一定最后跑。

#### Fault Isolation: 单通道隔离

```python
async def _safe_search(self, channel, context):
    try:
        return await asyncio.wait_for(
            channel.search(context),
            timeout=self._channel_timeout_s,
        )
    except asyncio.TimeoutError:
        return ChannelResult.empty(channel.channel_type())
    except Exception as e:
        logger.error(...)
        return ChannelResult.empty(channel.channel_type())
```

每个通道用 `wait_for` 设独立超时(默认 30s),**异常被吞成空结果**,
其他通道继续。这是 production 系统的基本要求 —— 一个外部 API
挂了不能让整个引擎挂。

#### 严格无状态 + Singleton Engine

引擎本身是无状态的(只持有 channels / processors 列表 + 超时配置),
所有上下文走 `SearchContext` dataclass。这意味着:

- 测试时随便换 mock context
- 多线程调用同一个 engine 没问题(channels 内部要自己处理并发)
- 重新加载配置只要重建 engine,不影响其他模块

工厂函数返回 process-wide singleton(lazy init):

```python
_engine: MultiChannelRetrievalEngine | None = None
def get_retrieval_engine():
    global _engine
    if _engine is None:
        _engine = create_retrieval_engine()
    return _engine
```

### 5. RRF(Reciprocal Rank Fusion)深入讲

#### 为什么 RRF 而不是加权平均?

不同通道的 raw score **量纲完全不一样**:

| Channel | Score 类型 | 范围 |
|---|---|---|
| ChunkVector | cosine similarity | [0, 1],典型 0.55-0.85 |
| Keyword (BM25) | Okapi BM25 | [0, +∞),典型 0-15 |
| SocialMedia | engagement score | log-scaled, 任意 |
| External | position-based | 1.0 - i*0.1 |

**直接加权平均没意义** —— BM25 的 5.0 和 cosine 的 0.5 不能直接比。
要做加权得先 normalize,而 normalize 本身又依赖于"整个语料库的 score 分布"
这种很难维护的统计量。

RRF 绕开这个问题:**只用 rank**。

#### RRF 公式

```
RRF_score(doc) = Σ  1 / (k + rank_in_channel_i)
                i ∈ channels containing doc
```

例子:doc_42 在 ChunkVector 排第 2、在 Keyword 排第 5,k=60:
```
RRF(doc_42) = 1/(60+2) + 1/(60+5) = 0.0161 + 0.0154 = 0.0315
```

#### 为什么 k=60?

来自 Cormack et al. 2009 原论文经验值。直觉:

- **k 小**(比如 1):top-1 vs top-10 差异巨大 → 极重视单通道高排名
- **k 大**(比如 1000):top-1 vs top-100 差异很小 → 重视"被多个通道同时召回"
- **k=60**:平衡点,业界沿用

要在你自己的标注集上 sweep,典型范围 30-100。我们没 tune,信任默认。

#### RRF 的好性质

1. **天然 cross-encoder agnostic**:任何能产生排名的检索方法都能进
2. **Score-scale free**:不用 normalize
3. **Stable under noise**:某通道某文偶尔排错位,RRF 影响有限
4. **Multi-channel boost**:被多个通道同时召回的文章自然得分更高

#### 我们实现里的 3 个细节

```python
# 1. 至少 2 个通道有结果才融合
if len(effective_results) < 2:
    return chunks  # 单通道不需要融合

# 2. 同一篇文章多通道版本:保留 score 最高的那个
if key not in chunk_map or chunk.score > chunk_map[key].score:
    chunk_map[key] = chunk

# 3. RRF score 归一化到 [0, 1] 给下游用
chunk.score = rrf_scores[key] / max_rrf
```

### 6. 后处理 4 步流水线

| Order | Processor | 作用 | 顺序的理由 |
|---|---|---|---|
| 1 | Dedup | URL 去重,priority-aware 选保留版本 | RRF 不该融合重复项 |
| 5 | RRF | 多路融合,产出统一 rank | 在 rerank 前先粗排 |
| 10 | Rerank | Cross-encoder 重排(Cohere/Jina) | 看完 RRF 的候选再精排 |
| 20 | QualityFilter | min_score + min_length 兜底 | 最后一道闸门,过滤垃圾 |

为什么 dedup 在 RRF 前?
**因为同一篇文章被多通道召回后,RRF 会在它名下累加多个 1/(k+rank)。
不去重的话,multi-channel boost 反而成了"重复加分"**,语义错的。

为什么 rerank 在 RRF 后?
**Rerank 是 cross-encoder,query·doc 双塔比对,比 RRF 准但贵 100x。
让 RRF 先把 top 30~60 选出来,reranker 只对这点候选做精排,$ 划算。**

### 7. 性能 / 延迟分析

实测 5 query 平均(取 K8s operator 那条):

```
Phase 1 总耗时:    ~7s   ── 由最慢通道决定(external)
  chunk_vector:    ~1.2s ── OpenAI embedding 1 次 + FAISS 查询
  article_vector:  ~1.4s ── 同上
  keyword (BM25):  ~10ms ── 纯内存
  social_media:    ~50ms ── MySQL 查询
  external:        ~7s   ── Google + DDG 网络 ★ 瓶颈
Phase 2 总耗时:    ~10ms
  Dedup:           ~2ms
  RRF:             ~3ms
  Rerank:          0     ── 没启用
  QualityFilter:   ~1ms
─────────────────────────
端到端:            ~7s
```

**主要瓶颈是 external**(占 80%+)。优化方向:

1. 把 external 改成"chunk_vector 命中 < N 时才触发"的条件通道
2. 给 external 加更短的 timeout(目前 30s 太宽)
3. 把 Google News 和 DDG 拆成两个独立通道并行(目前内部串行)

### 8. 这套架构的局限

诚实列一下:

1. **没正经评测集** —— 5 query + 肉眼判断不是评估
2. **没有 query rewriting** —— "fuzzy topic" 直接喂 BM25/embedding,
   有 query → query 改写空间(LLM 把"AI 创业融资"改写成多个子查询)
3. **rerank 默认没启用** —— config 里有 toggle 但没接 API key,
   "transformer attention" 这条引出 ransomware 文那种典型 BM25 误召没人拦
4. **没有意图分类** —— 5 个通道每条 query 都跑,有些通道明显不该跑
   (比如知识型 query 不需要 social_media)。可以加 intent gate
5. **single-process 单实例** —— 多进程时每个 worker 自己一份 BM25 索引,
   内存浪费。需要时该拆 sidecar
6. **External 通道延迟拖累整体** —— 见 §7

---

## 进阶追问(技术深度题)

### 系统设计

#### Q1: 为什么是 5 个通道,不是 3 个或 10 个?

A:经验性选择,**每加一个通道要回答两个问题**:
"它能召回别的通道召回不到的内容吗?"和"它的延迟/成本能容忍吗?"

- 2 个向量通道(chunk + article)给了**召回粒度**的双层
- 1 个 BM25 给了**字面量**互补
- 1 个 social 给了**实时短文本**
- 1 个 external 给了**库外兜底**

再加 reddit / hackernews / twitter API 这些就是同类的扩展,
价值递减。要加 story clustering 这种**新维度**才值得。

#### Q2: 为什么不在每个通道里直接 rerank,而是统一到 post-process?

A:**职责分离 + 成本**。

- 通道做"召回",rerank 做"精排",混在一起会让单通道延迟暴涨
- Rerank API 按 query·doc 数计费,在融合后调一次比每通道各调一次便宜 N 倍
- Rerank 是 cross-channel 公平的(它不知道 chunk 来自哪)

#### Q3: SearchContext / RetrievedChunk 这套数据流为什么用 dataclass 不用 dict?

A:三个理由:

- **类型安全** —— `chunk.score = 0.5` vs `chunk["socre"] = 0.5`(typo 编译时不报)
- **演化友好** —— 加新字段(metadata、source_channel)是加 field 不是改 dict 协议
- **不可变性约定** —— 后处理器之间约定"chunks 列表可以重排但 chunk 内部字段不要乱改",dataclass 文档化这个约定

#### Q4: 5 个通道并行,为什么不也把后处理并行?

A:后处理之间**有依赖关系**:

```
Dedup → RRF (RRF 不能融合重复)
RRF → Rerank (rerank 想要的是 RRF 后的 top 30,不是原始 60)
Rerank → QualityFilter (filter 看的是 rerank 后的最终分)
```

强行并行会破坏语义。后处理是**链式 + 顺序**的本质决定了它必须串行。

### 算法 / 检索

#### Q5: 为什么用 RRF 而不是 weighted average?

A:见 §5 详细。一句话总结:**weighted average 需要 normalize 不同通道的 raw score,而 normalize 本身就是开放问题。RRF 用 rank 绕开,无 free parameter (k 也只是 smoothing)。**

#### Q6: RRF 有什么缺点?什么时候不该用?

A:

1. **完全忽略 raw score 信息** —— 通道 A 的第 1 名 cosine=0.99,
   通道 B 的第 1 名 cosine=0.51,RRF 给同样的 1/(k+1)。
   有时候 raw score 的差异是真信号
2. **对"全部通道都召回"的文章过度奖励** —— 一篇泛泛的热门文章可能在
   所有通道排名都中等,RRF 反而把它推到 top
3. **不能学习** —— 它是固定公式,不会随用户反馈调整。
   如果你有大量点击数据,**learning to rank**(LTR) 会比 RRF 强

什么时候不该用 RRF:**有标注数据 + 量足够大** 时,直接训 LightGBM/LambdaMART rerank model 替代 RRF + cross-encoder。

#### Q7: 如果加一个通道,RRF 能立刻 work 吗?

A:能。这就是 RRF 的优雅之处 —— 它是**通道无关**的,加一个通道
只要它能产生 rank 就直接进融合。
加权平均的话还要重新调权重 + 重新 normalize,改动面大得多。

### 并发 / 工程

#### Q8: asyncio.gather vs ThreadPoolExecutor 怎么选的?

A:gather 因为:
1. 通道里大量是 IO 等待(OpenAI / FAISS / MySQL / HTTP),asyncio 天然适合
2. 单进程内调度成本远低于线程切换
3. Python GIL 下 ThreadPoolExecutor 对 CPU-bound 没用,IO-bound 跟 asyncio 等价

例外:**FAISS 查询是 CPU-bound C 扩展**,在协程里会阻塞事件循环。
我们的代码里 ChunkVectorChannel 内部已经用 `asyncio.to_thread()`
把同步 FAISS 调用扔到线程池(看 [chunk_vector_channel.py](../agent/rag/channels/chunk_vector_channel.py))。

#### Q9: 单通道异常被吞,会不会 silent failure 让 bug 永远不被发现?

A:**会**,这是真实的代价。我的对冲是:

- `_safe_search` 里 `logger.error(...)` 把异常打到日志
- engine 在 phase 1 结束时打每通道的命中数,长期 0 命中会被 probe 发现
- 通道结果带 `latency_ms`,可以接监控看分位线

production 应该再加:
- 通道失败计数器 → Prometheus 指标
- 连续 N 次失败触发告警
- 健康检查端点暴露每通道的最近成功率

我没做这些,因为目前是个人项目。但**架构留好了挂这些的位置**。

#### Q10: 如果一个通道 hang 住但不超时(比如卡在 GIL 里),会怎样?

A:`asyncio.wait_for` 是基于事件循环的,不能强杀阻塞 GIL 的任务。
所以理论上一个通道里写个死循环会拖垮整个 event loop,
其他通道也跑不完。

**对策**:CPU-bound 的通道(FAISS)用 `to_thread` 隔离到线程池,
让事件循环保持响应。这是 §Q8 提到的 FAISS 处理方式的另一面。

### RAG 整体演化

#### Q11: 这套架构的下一步演进是什么?

A:按性价比排:

1. **接入 reranker(Cohere / BGE)** —— 当前 rerank processor 是空架子,
   接 API 立刻能解决"transformer attention 召回到 ransomware 文"这种 false positive
2. **降低 ArticleVector 阈值 0.70 → 0.55** —— 5 分钟改动,3/5 query 该通道立刻活过来
3. **External 改条件触发** —— 减 80% 延迟
4. **Query rewriting** —— LLM 把模糊 topic 改写成多个精确子查询,fanout 给所有通道
5. **Story clustering** —— 把同一新闻事件多源文章聚合,给 RAG 提供 saliency 信号
6. **Intent classification + 通道动态启停** —— 知识型 query 不跑 social,时效型 query 不跑 article_vector
7. **Learning to rank** —— 有了用户行为数据后替换 RRF + cross-encoder

注意 1-3 是低成本高收益,4-7 都是新建能力,要看产品 SLA。

#### Q12: 这套是不是 over-engineering?直接 dense vector + reranker 不够吗?

A:**对小项目可能过度,对当前需求恰好。**

判断标准:
- 我们的内容混合了**库内文章 + 实时新闻 + 社交反应**,本质是多源
- 用户 query 既有"GPT-4o"这种字面量,也有"AI 创业融资"这种概念
- 可用性要求"任一外部 API 挂了不能整体挂"

dense + reranker 的简化版能解决 70%,剩下的 30%(字面量、库外、容错)
就是多通道存在的理由。**多通道架构的复杂度成本 ~3 天一次性投入,
长期收益是召回质量 + 可演化性。**

#### Q13: 行业里有类似架构吗?

A:这是 hybrid retrieval 的标准玩法,不少公司公开过类似设计:

- **Anthropic Constitutional AI / RAG best practices** docs 里推 BM25 + dense
- **OpenAI Cookbook** 的 hybrid search 例子是 ES BM25 + cosine + RRF
- **Vespa / Marqo / Weaviate** 这些向量数据库都内置了 BM25
- **Pinecone** 有 hybrid sparse-dense 模式
- **Cohere** 的 RAG 文档明确推荐 BM25 + dense + rerank 三段式

我们的实现就是这个标准架构的 **Python 自建版**,
没用 Vespa / ES 是因为规模不够大(见 BM25 文档 Q12 三阶段路线图)。

---

## 简历上怎么写

短版(一行):
> 设计实现 Python 多路 RAG 检索引擎(5 channel + 4 post-processor),
> 支持 fault isolation、RRF 跨通道融合、链式后处理,被播客生成 pipeline 调用。

详版(2-3 行):
> 设计并实现多路并行 RAG 检索引擎:5 个通道(2 向量 + BM25 + social + external)
> 通过 asyncio 并行执行并独立超时,4 个 post-processor(dedup → RRF → rerank → quality)
> 链式融合。RRF 用 rank-based 融合解决跨通道 score 量纲不可比问题。
> 支持单通道故障隔离、配置驱动的通道启停、可插拔扩展(加新通道改 3 行代码)。

关键 keyword(招聘方爱看):
- **multi-channel retrieval / hybrid search**
- **RRF / Reciprocal Rank Fusion**
- **fault isolation / async parallelism**
- **chain of responsibility / strategy pattern**
- **BM25 + dense embedding hybrid**

---

## 关键文件 / commit

- 架构主体: [`agent/rag/engine.py`](../agent/rag/engine.py),[`agent/rag/factory.py`](../agent/rag/factory.py),[`agent/rag/models.py`](../agent/rag/models.py)
- 5 个通道: [`agent/rag/channels/`](../agent/rag/channels/)
- 4 个后处理: [`agent/rag/postprocessors/`](../agent/rag/postprocessors/)
- 单元测试: [`agent/tests/test_rag_engine.py`](../agent/tests/test_rag_engine.py)
- 诊断工具: [`agent/scripts/probe_channels.py`](../agent/scripts/probe_channels.py)
- 主体 commit: `98e3386` "Add multi-channel retrieval engine with RRF fusion"
- BM25 子优化: `26ab740` (详见 [bm25-keyword-channel.md](./bm25-keyword-channel.md))
