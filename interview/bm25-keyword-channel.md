# 用 BM25 重写多路检索的 Keyword 通道

> 一次"先量化诊断、再选型落地、再验证收益"的检索优化经历。
> 适合放在简历项目里讲 30–60 秒,也能展开聊 5–10 分钟深入题。

---

## 一句话版本(电梯)

> 在多路 RAG 检索引擎里把字面量召回通道从 SQL `LIKE '%query%'`
> 改成 in-memory Okapi BM25,通道命中率从 0/5 query → 5/5,延迟从
> 几百 ms / 全表扫降到 6–16ms / 内存查询,整套方案没引入任何外部
> 服务依赖。

---

## 30 秒版本(自我介绍式)

我们的播客生成项目里有一个 **多路并行检索引擎**(MultiChannelRetrievalEngine),
跑 5 个通道:chunk-level FAISS 向量、article-level FAISS 向量、keyword、social media、
external news,最后用 RRF 融合。我做了一个诊断脚本(probe_channels.py),
用 5 条代表性 query 跑全 pipeline,发现 keyword 通道**5/5 全 0 命中**——
原因是它用的是 `LIKE '%整条 query%'` 做整串子串匹配,多词 query 永远命不中。

我直接换成 **Okapi BM25**(rank-bm25 库),内存索引、TTL 10 分钟刷新、
两段式查询(BM25 排名 → SQL hydrate)。改完之后每条 query 都能召回
7–20 条字面量相关结果,延迟 6–16ms,RRF 融合的活跃通道数从 2–3 提到 3–4。

整个改动 4 个文件、377 行新增,没引入任何外部服务。

---

## 完整版本(技术深聊,适合现场展开)

### 1. 背景:为什么需要 keyword 通道

播客脚本生成场景下,LLM 写稿前要召回多源材料。我们用了多路检索:

| 通道 | 数据源 | 解决什么 |
|---|---|---|
| ChunkVectorChannel | FAISS chunk index | 段落级语义召回(主力) |
| ArticleVectorChannel | FAISS article index | 文章级宽召回 |
| **KeywordChannel** | MySQL crawled_articles | 字面量(GPT-4o, K8s 这类向量糊掉的 token) |
| SocialMediaChannel | social_media table | 实时社交反应 |
| ExternalSearchChannel | Google News + DDG | 库外兜底 |

向量通道对**语义近似**强,但对**精确字面量**弱(embedding 把 "GPT-4o"
压成稠密向量,跟 "GPT-5" 距离很近)。所以需要一个独立的 lexical 通道
互补 —— 这是 BM25 的天职。

### 2. 问题发现:先量化,再动手

我没有直接拍脑袋说"keyword 通道有问题",而是先写了诊断脚本
[`scripts/probe_channels.py`](../agent/scripts/probe_channels.py):

- 5 条覆盖不同 query 类型的代表性话题(字面量型 / 时效型 / 知识型 / 实体型)
- 每条 query 调 `rag_search()` 走真实 pipeline
- 输出每通道命中数 + 延迟 + final top-10 通道分布

**结果一目了然**:

| Query | chunk | article | **keyword** | social | external |
|---|---|---|---|---|---|
| GPT-4o multimodal | 15 | 2 | **0** | 0 | 13 |
| AI startup funding | 14 | 13 | **0** | 0 | 13 |
| transformer attention | 15 | 0 | **0** | 0 | 13 |
| Sam Altman | 13 | 0 | **0** | 0 | 13 |
| K8s operator | 10 | 0 | **0** | 0 | 13 |

keyword 5/5 全 0,这才是数据驱动的根因切入点。

### 3. 根因:为什么 LIKE 跑不通

打开旧实现一看:

```sql
WHERE ca.title LIKE %s OR ca.content LIKE %s OR ca.summary LIKE %s
-- params: like_term = f"%{query}%"
```

三个问题一起出现:

1. **整条 query 当一个子串匹配** —— `%AI startup latest funding rounds%`,
   文章里**永远不会有这一整串原话**
2. **`LIKE '%xxx%'` 用不上 B-tree 索引** —— 全表扫,O(N)
3. **没相关性评分** —— 拍脑袋给 title 1.0、content 0.8,排序按时间戳,
   完全不是相关性排名

**这不是 keyword search,是 substring search,且实现上还很糙。**

### 4. 选型:为什么 BM25,为什么 in-memory

候选方案我比较了三种:

| 方案 | 真 BM25? | 新增依赖 | 维护成本 | 选择理由 |
|---|---|---|---|---|
| MySQL FULLTEXT | ❌(TF·IDF²) | 无 | 低 | 不是真 BM25,跟向量通道融合时算分不公平 |
| Elasticsearch / Meili | ✅ | 多一个服务 | 高 | 当前规模(几万篇文章)是 overkill |
| **rank-bm25 内存索引** | ✅(Okapi) | 一个 pip 包 | 低 | **选这个** |

关键判断:

- **真 BM25 才能跟向量通道在 RRF 里公平融合** —— MySQL FULLTEXT 的
  TF·IDF² 跟向量相似度量纲完全不同,RRF 虽然只看 rank 不看分,但选型要诚实
- **几万篇文章的 corpus 在内存里只占几十 MB**,完全不是 ES 的应用场景
- **避免引入新服务进程** —— 多一个 Elasticsearch 就多一份运维

### 5. 实现:in-memory + TTL + 两段式查询

#### 模块边界

```
rag/keyword_index.py        ← BM25 单例索引(新)
rag/channels/keyword_channel.py  ← Channel 实现(重写)
```

为什么把索引和 channel 分开?**因为索引可以被多种检索策略复用**
(未来加意图过滤、二级 BM25、混合 BM25+向量等都不用改 channel)。

#### BM25 索引核心设计

```python
class BM25Index:
    def __init__(self, *, ttl_seconds: float = 600.0):
        self._snapshot: Optional[_IndexSnapshot] = None
        self._lock = threading.Lock()  # double-checked locking

    def search(self, query, *, top_k=20) -> list[(article_id, score)]:
        snapshot = self._get_or_build()  # lazy + TTL
        scores = snapshot.bm25.get_scores(_tokenize(query))
        # numpy.argsort 取 top-k,过滤 score=0
```

四个关键点:

- **Lazy load** —— 首次查询时才构建,不拖慢启动
- **TTL 刷新(10 分钟)** —— 新文章入库会有最多 10 分钟的延迟可见性,
  权衡了"实时性"和"重建成本",新闻聚合场景可接受
- **Double-checked locking** —— 高并发下避免重复构建,但热路径无锁
- **不可变 snapshot** —— 重建时构造新的 `_IndexSnapshot`,原子替换,
  正在跑的 query 不受影响

#### Channel 两段式查询

```python
# Stage 1: BM25 拿排名(纯内存计算,~10ms)
hits = get_bm25_index().search(query, top_k=20)
article_ids = [aid for aid, _ in hits]

# Stage 2: SQL hydrate 拿元数据(一次 IN 查询,~5ms)
rows = SELECT id, title, url, summary FROM crawled_articles WHERE id IN (...)

# 重要:DB IN(...) 返回无序,要按 BM25 score 重新排
ordered_rows = [rows_by_id[aid] for aid in article_ids]
```

为什么不一步到位?**因为 BM25 索引不该存原文**(那是数据库的活)。
索引只存 token,DB 只存内容,职责清晰。

#### Tokenizer 选择

```python
_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+")
```

简单的字母数字 token 化,英文够用。CJK 内容要换 jieba。这是个有意识的
**先解决 80% 场景**的决策,代码注释里也写了。

### 6. 验证:probe 数据前后对比

| Query | keyword 之前 | keyword 现在 | 进 top-10 |
|---|---|---|---|
| GPT-4o multimodal | 0 | **20** | 1 |
| AI startup funding | 0 | **8** | 0 |
| transformer attention | 0 | **20** | 2 |
| Sam Altman | 0 | **7** | 1 |
| K8s operator | 0 | **7** | 3 |

- 通道命中率: 0/5 → **5/5**
- 延迟: 几百 ms 全表扫 → **6–16ms 内存查询**
- RRF 活跃融合通道数: 2–3 → **3–4**(更高融合多样性)

### 7. 我知道这套方案的边界

**会扛不住的几种场景**(也是面试官可能追问的点):

1. **corpus 涨到百万级** —— 50MB / 1 万文章估算,百万级要 5GB 内存,
   超出单机能扛的范围。届时该上 Elasticsearch 或 Tantivy
2. **要求实时索引(秒级新文章可见)** —— TTL 模型不够,要换成增量索引,
   或者干脆上有正经 inverted index 增量构建能力的产品
3. **CJK 占主导** —— 现在的 regex tokenizer 把中文扔了,要换 jieba 或
   pkuseg,token 化质量直接决定 BM25 效果
4. **多语言混合 + 同义词扩展** —— 这是 BM25 的天花板,需要 LLM-based
   query rewriting 或 sparse + dense hybrid embedding(SPLADE 这类)

### 8. 这次没做但识别到的下一步

probe 的数据还暴露了两个问题,我没在这一轮一起改,因为想保持改动聚焦:

1. **ArticleVectorChannel 阈值 0.70 过严**,3/5 query 命中 0 —— 应降到 0.55
2. **"transformer attention" query 把 "ransomware quantum-safe" 排进 top-10**
   —— 这是典型 BM25 误召(都含 "transformer" 但语义无关),
   该补一个 reranker(cohere-rerank / bge-reranker)

把这些列出来,本身就是体现"知道还有什么要做、不会过度工程化"的信号。

---

## 面试官可能问什么(及怎么答)

### Q1: 为什么不用 Elasticsearch?

A:加一个服务进程会带来运维成本(部署、监控、容量、升级),
而当前 corpus 规模(几万篇文章、几十 MB)放内存绰绰有余。
**复杂度要匹配规模**,过早上 ES 是为了简历好看,不是为了产品好用。
真到了百万级、需要复杂查询语法或分词插件时,再迁也不迟,
而且届时数据已经积累足够,迁移决策更扎实。

### Q2: TTL 10 分钟期间新文章看不到,业务能接受?

A:这是新闻聚合场景的特性 —— 文章入库到能被检索到,
本来就有抓取 → 清洗 → embedding → 入索引的链路延迟。
10 分钟在这个 pipeline 里不是瓶颈。如果业务方反馈"最新热点没来得及检索",
有两个解法:① 调小 TTL;② 加 `force_rebuild()` 钩子在新文章入库时触发。
**但我先不做**,因为没有数据证明这是问题,YAGNI。

### Q3: BM25 跟向量怎么融合?权重怎么调?

A:用 RRF(Reciprocal Rank Fusion),`score = Σ 1/(k + rank_i)`,
默认 k=60。RRF 的好处是**不依赖原始 score 的量纲**,只看每个通道
里的排名 —— BM25 给 5.7 分、cosine 给 0.83 分,在 RRF 看来都是
"通道内排第几"。这跟我"诚实选型 BM25 而不是 MySQL FULLTEXT"是一脉相承的:
我想保留每个通道的独立信号,让 RRF 做公平融合,而不是混进个山寨 BM25。

### Q4: 为什么要做 probe 脚本而不是写单元测试?

A:**单元测试和探针解决不同问题**。单元测试用 mock channel 验证
RRF 算法、错误兜底等**逻辑正确性**;probe 用真 DB / 真 OpenAI
验证**召回效果**。光有单元测试,我永远不会发现 keyword 5/5 全 0 ——
那是运行时数据问题,不是代码 bug。
两者互补:单测进 CI,probe 在每次改检索层后手动跑一次。

### Q5: 这套方案撑得住多大流量?

A:BM25 query 是纯内存 numpy,O(N·avg_tokens),几万文章下单查询
~10ms。瓶颈不在 BM25,而是:① 索引重建 cost(冷启动 + TTL 过期),
1 万文章构建 ~500ms;② SQL hydrate 那一段是 IN(...) 查询,会随着 top-k 线性增长。
QPS 上万的话该把 BM25 索引拆成 sharded、做副本、或者直接换 ES。
当前体量(个人项目 / 中小流量)单机内存版完全够。

---

## 进阶追问(技术深度题)

下面这些问题是面试官真正想看你 IR / 系统/ 评估三方面深度时
会出的题。有些我**不会装懂**——能答的好好答,答不出的会大方说
"这个我没深做过,但我会怎么去查"。

### 算法 / IR 基础

#### Q6: BM25 公式具体长什么样?k1 和 b 是干嘛的?

A:Okapi BM25 单 term 评分:

```
score(D, q) = IDF(q) · (f(q,D) · (k1+1)) / (f(q,D) + k1 · (1 - b + b · |D|/avgdl))
```

- `f(q,D)`: term q 在文档 D 出现的次数
- `|D|`: 文档长度,`avgdl`: 平均文档长度
- **`k1`(默认 1.2~2.0)**: 控制词频饱和速度。`k1 → 0` 就退化成"出现/不出现"的二值;`k1 → ∞` 就接近线性 TF。BM25 的精髓是 **TF 饱和**——同一个词出现 5 次比 1 次相关性更高,但比 100 次的边际收益已经很小,这一点 TF-IDF 不会做。
- **`b`(默认 0.75)**: 长文档惩罚强度。`b=0` 不惩罚长文(纯 TF·IDF 行为),`b=1` 完全按相对长度归一化。新闻文章长度差异不大,默认值 OK。

rank-bm25 的 `BM25Okapi` 默认 `k1=1.5, b=0.75`,我没改。

#### Q7: BM25 比 TF-IDF 到底强在哪?

A:两点本质区别:

1. **TF 饱和**(上面 Q6 讲了)—— TF-IDF 是 `tf · idf`,词频线性叠加,
   一篇 spam 把同一个词重复 100 次就赢了。BM25 加了饱和函数,这种作弊不灵
2. **长度归一化**—— TF-IDF 不做或做得粗糙(用 1/|D|),长文档天然吃亏。
   BM25 的 `b · |D|/avgdl` 是软归一化,可调

经验上 BM25 在 TREC 这类公开评测里持续比 TF-IDF 高 2–5 个百分点 nDCG,
是 IR 领域的事实标准。

#### Q8: 你的 tokenizer 把什么扔了?会有什么后果?

A:看 [`keyword_index.py`](../agent/rag/keyword_index.py) 里的 `_TOKEN_PATTERN = r"[a-zA-Z0-9]+"`:

- ❌ 标点(包括连字符)—— "GPT-4o" 变 ["gpt", "4o"],"state-of-the-art" 变 4 个 token。可能误伤
- ❌ 停用词(the, a, is...)—— 没去,但 BM25 的 IDF 会自动给它们超低权重,所以问题不大,只是 corpus 大了内存浪费
- ❌ Stemming(running → run)—— 没做,"running" 和 "run" 不会互通
- ❌ CJK —— 中文字符直接被 regex 扔了,中文 query 会变空

**取舍是有意的**:数据是英文 tech news,简单 tokenizer 80% 够用。
要做正经多语言要换 ICU tokenizer / pyicu / spaCy / jieba(中文)。

### 工程 / 并发

#### Q9: 你的 double-checked locking 真的对吗?Python 里有坑吗?

A:在 Python 里这个模式比 Java 安全很多,因为:
1. CPython 的 GIL 保证单条字节码原子性,`self._snapshot = new_snapshot` 是一条 STORE_ATTR
2. 我读取的是 dataclass 实例(immutable),不存在"半构造对象"问题
3. `_IndexSnapshot` 是值对象,赋值即发布

潜在坑:**热路径无锁读 `self._snapshot`**,理论上跨 CPU 缓存可能读到老值。
但 CPython GIL 实际上是个 process-wide lock,这个问题不会发生。
迁到 PyPy(无 GIL)或多进程时要重新评估。

#### Q10: 索引重建那 500ms 期间,如果有 query 进来会怎样?

A:看代码:

```python
def _get_or_build(self):
    snap = self._snapshot
    if snap is not None and (now - snap.built_at) < ttl:
        return snap   # 热路径,无锁
    with self._lock:  # 冷/过期才进锁
        ...
```

- **第一次 query**:必须等构建完(锁内串行),~500ms。可优化:启动时 eager build
- **TTL 过期后第一个 query**:同样要等。可优化:后台线程提前重建
- **重建期间其他 query**:用**旧快照**(`self._snapshot` 还没替换),
  不阻塞,只是数据稍陈旧 —— 这是有意的设计

#### Q11: 为什么用 numpy.argsort 不用 heap?

A:argsort 全排 O(N log N),heap top-k O(N log k)。
理论上 N=10000、k=20 时 heap 应该快 5–10x。

实测下来 numpy 的 SIMD 优化让 argsort 在 10万级以内反而更快(C 实现 + 缓存友好),
而 heap 在 Python 层有解释器开销。
**我没真做基准测试**,但 rank-bm25 的 `get_top_n` 也是这么写的,
信任社区选择。
如果 corpus 涨到百万级,该换 `np.argpartition` 取 top-k 不全排。

### 系统设计 / 扩展

#### Q12: 怎么从内存索引扩到分布式?

A:三个阶段:

1. **单机 → 持久化** —— 把 BM25 序列化到磁盘(rank-bm25 不直接支持,
   但可以 pickle 或用 `bm25s` 库的 sparse 矩阵存储)。冷启动从磁盘加载 ~50ms
2. **持久化 → sharding** —— 按 article_id 哈希分片,每个分片一个 BM25,
   query 时 fanout 到所有分片再合并 top-k。Python 实现就是多进程
3. **sharding → ES / Vespa** —— 到几千万级文档,自己维护增量、删除、
   re-index 不划算了,直接用 ES。届时 BM25 从我们的代码里消失,
   变成 ES 的内部细节

每一步都对应一个真实的临界点(corpus 大小、QPS),不要提前跳

#### Q13: 怎么做增量索引(新文章入库立刻可见)?

A:rank-bm25 的 BM25Okapi 是不可变的,加文档要全重建。
要支持增量,有两条路:

1. **双缓冲 + 异步重建** —— 后台 N 秒触发一次重建,前台用旧索引。
   实现简单,但仍有 N 秒延迟
2. **真增量 BM25** —— 维护 `df`(doc frequency)、`tf` 字典、`avgdl`,
   加新文档时只更新这些统计量。`bm25s` 这类库支持。但删除/更新文档要小心 IDF 漂移
3. **直接换 Lucene-based 引擎** —— ES / Tantivy / Whoosh 自带增量

当前 TTL 模型是有意识的简化,业务能接受 10 分钟陈旧。
**我会先问产品"实时性 SLA 是什么"再决定走哪条**。

#### Q14: 文章被删除 / 更新怎么办?

A:当前 TTL 模型下:被删除的文章在下次重建前还在索引里,
SQL hydrate 时会发现 ID 不存在 → 这一项被静默丢弃(`rows_by_id` lookup miss)。
所以**删除是 eventually consistent 的**,最多 10 分钟脏数据。

更新更微妙:文章内容改了但 BM25 还按老 token 排序。
不算 bug,只是 stale。同样下次 rebuild 修复。

要立即一致就用 Q13 的方案 2 或 3。

### 评估 / 调优

#### Q15: 没有标注集,你怎么证明 BM25 比 LIKE 好?不是只是"召回多了"?

A:**这是好问题,我没有真正做评测**,只用 5 条 query + 肉眼判断。
诚实说法是:**我证明了 LIKE 没在工作(5/5 全 0),
没证明 BM25 排序质量好**。

要做严肃评测应该:
1. 准备 50–100 条 query 标注理想 top-3
2. 算 recall@10、nDCG@10、MRR
3. 跑 LIKE / BM25 / hybrid 三种配置对比

但**没做不等于不知道怎么做**。当前阶段瓶颈是 LIKE 完全坏掉,
修了之后任何能跑的 BM25 都是改进。等到要在 BM25 / FULLTEXT /
ES 三个方案之间做精细化决策时,那时再投入做评测集。
**资源要花在边际收益最大的地方。**

#### Q16: top-10 里出现 "ransomware quantum-safe" 这种误召,怎么 debug?

A:看错在哪一层:

1. **BM25 阶段**(channel 输出 20 条) —— 这条进来了说明它的 BM25 score 不低
2. **RRF 阶段** —— 把它跟 chunk_vector 的 rank 融合,可能侥幸进 top
3. **QualityFilter 阶段** —— 没拦住

debug 步骤:
- 把这条文章的内容拉出来,看是哪些 token 让 BM25 觉得相关("transformer" 在
  IT 安全文章里也常出现,因为 ML transformers 已经渗透到所有领域)
- 看 `bm25_score` 元数据,如果只是边缘相关就改阈值
- 根本解法:**加 reranker** —— BM25 的 lexical 分跟语义无关,
  cohere-rerank-v3 / bge-reranker 这类 cross-encoder 能直接判 query-doc 语义相关性,
  把这种 false positive 砍掉

### RAG 整体架构

#### Q17: 为什么多路检索 + RRF,不直接用 dense vector + reranker?

A:三个理由:

1. **召回多样性**—— Dense vector 对**语义近似**强但对**精确字面量**弱
   (它把 "GPT-4o" 和 "GPT-5" 看得很近)。BM25 在字面量上完胜。两者互补
2. **降低单点失败**—— 一个通道挂了(API 超时、索引损坏)还有别的扛着。
   单路系统一挂全挂
3. **成本**—— Reranker(尤其调 API 的)按 query·doc 数计费,
   先用便宜的多路检索过滤候选,再 rerank top-K,$ 划算

行业实践叫 **hybrid retrieval + late-stage rerank**,
是当前 RAG 的事实标准架构(参考 Anthropic / OpenAI / Cohere 的 best practices)。

#### Q18: RRF 的 k 为什么是 60?

A:这是原论文 (Cormack et al. 2009) 的经验值,业界沿用至今。
直觉:`score = 1/(k + rank)`,当 k 较大时不同 rank 之间的差距被压平,
**让排在靠后的候选也有机会被融合带上来**;k 较小则只重视 top-3 左右。

k=60 在 N=10~100 的候选规模下经验上效果稳定。
要严格 tune 就在标注集上 sweep k ∈ {10, 30, 60, 100, 200}
看 nDCG。我没 tune,信任默认值。

#### Q19: BM25 已经过时了,现代检索都用 dense embedding 了,你怎么看?

A:这是错误叙事。BM25 没过时,而是**变成 hybrid 系统的一个组件**:

- **Dense-only 在 OOV(out-of-vocabulary)、长尾实体、code search 这种
  字面量场景上仍然不如 BM25**
- 顶级 IR 系统(Vespa、Elasticsearch、Marqo)都同时跑 BM25 和 dense
- 学术上有 SPLADE / ColBERT / Sparse Embedding 这些"用 NN 学一个稀疏表示"
  的工作,本质还是 BM25 思路的变体

我的方案就是 hybrid 的标准玩法:BM25(KeywordChannel) +
Dense(Chunk/ArticleVectorChannel),用 RRF 融合。
**真正过时的是"只用 LIKE",不是 BM25。**

---

## 简历上怎么写一句

> 在播客生成项目的多路 RAG 检索引擎中,通过自建 probe 脚本量化诊断
> 出 keyword 通道命中率 0%,定位为 SQL LIKE 整串子串匹配的实现缺陷,
> 用 in-memory Okapi BM25 重写,实现 5/5 query 召回 + 6–16ms 内存查询,
> 无新增外部服务依赖,RRF 活跃融合通道数从 2–3 提至 3–4。

关键卖点(招聘方爱看的几个 keyword):
- **数据驱动**(probe 量化诊断)
- **选型权衡**(对比了 MySQL FULLTEXT / ES / rank-bm25,有理有据)
- **真 BM25 不是 TF·IDF²**(展示 IR 基础知识)
- **不过度工程化**(没引入新服务,匹配当前规模)
- **知道边界**(明确说出何时该升级到 ES)

---

## 关键 commit / 文件

- Commit `26ab740` — `replace LIKE-based KeywordChannel with in-memory BM25`
- 新增: [`agent/rag/keyword_index.py`](../agent/rag/keyword_index.py)
- 重写: [`agent/rag/channels/keyword_channel.py`](../agent/rag/channels/keyword_channel.py)
- 诊断: [`agent/scripts/probe_channels.py`](../agent/scripts/probe_channels.py)
- 依赖: `rank-bm25==0.2.2`
