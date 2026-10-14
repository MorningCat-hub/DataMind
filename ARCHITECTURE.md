# DataMind 架构说明

> 本文描述**当前**代码结构。
> 仓库里原有的 `整体架构.txt`（其实是旧版 `state.py` 的拷贝，还带着早就不存在的
> `is_finished` 字段）和 `文件夹架构.txt`（datamind-v3 的目录树，还列着已删除的
> `chart_tool.py`）已经移除。

## 一句话

把「上传一个数据文件 + 用中文问一个问题」，变成一份**数值可核对、结论有依据**的分析报告。

## 处理链路

14 个 LangGraph 节点，`AgentState` 是唯一的数据总线。

```
文件
 │
 ├─ dataset          多格式读取，统一归一化成 CSV
 │
 ├─ health_check     （V4.11）数据体检：确定性排查数据的局限
 │
 ├─ retrieve         （V4.6）检索业务口径，注入 Planner 和 Executor
 │
 ├─ planner          把问题拆解成可独立执行的分析任务
 │
 ├─ executor         （V4.5）原生 Tool Calling 循环
 │     │
 │     ├─ 成功 ──────────────────────────────┐
 │     │                                     │
 │     └─ 失败 ─► error_handler               │
 │                  │  生成修复建议           │
 │                  ├─► 还能重试 ─► executor  │
 │                  └─► 超过上限 ─► task_failure（放弃该任务）
 │                                            │
 │◄───────────────────────────────────────────┘
 │
 ├─ reviewer         审查分析是否完备，可要求补充任务（最多 2 次全局重规划）
 │     └─ 需要重规划 ─► retry ─► planner
 │
 ├─ chart_planner    判断是否需要图表、规划图表
 ├─ chart_executor   渲染 PNG
 ├─ report           生成最终报告
 ├─ verify           （V4.4）核对报告里的数值是否可追溯到计算结果
 └─ conclusion_check （V4.12）核对报告结论是否与数据限制冲突
```

三层自愈：

| 层级 | 机制 | 上限 |
|---|---|---|
| 任务内 | 工具报错回填给模型，模型自己改参数重试 | 单任务 8 轮工具调用 |
| 任务级 | `error_handler` 生成修复建议后重跑该任务 | 每任务 2 次 |
| 全局 | `reviewer` 判定不完备 → `retry` → 重新规划 | 2 次 |

## 目录结构

```
datamind-v4.0/
├── main.py                      FastAPI 入口、SSE 流式接口、会话 API、临时目录清理
├── demo_orders.csv              演示数据（含退款/折扣/订单状态，用于口径演示）
├── test_chart_data.csv          绘图链路的测试数据
│
├── agent/
│   ├── state.py                 AgentState（唯一数据总线）
│   ├── graph.py                 StateGraph 组装 + 路由函数
│   ├── nodes/                   14 个节点
│   │   ├── dataset.py           多格式读取 → 统一 CSV
│   │   ├── health_check.py      （V4.11）数据体检
│   │   ├── retriever.py         业务口径检索
│   │   ├── planner.py           任务规划
│   │   ├── executor.py          工具调用循环
│   │   ├── error_handler.py     错误分析与修复建议
│   │   ├── task_failure.py      任务放弃记录
│   │   ├── reviewer.py          结果审查
│   │   ├── chart_planner.py     图表规划
│   │   ├── chart_executor.py    图表渲染
│   │   ├── reporter.py          最终报告
│   │   ├── verifier.py          数值核对
│   │   └── conclusion_check.py  （V4.12）结论级核对
│   └── utils/
│       ├── health_check.py      （V4.11）数据体检实现（6 类检查）
│       ├── conclusion_check.py  （V4.12）结论核对实现（3 条规则）
│       ├── verifier.py          报告数值核对实现
│       ├── knowledge.py         口径检索实现（词法 + LLM 兜底 + 适用性筛选）
│       ├── time_granularity.py  时间粒度识别与降级
│       ├── chart_*.py           绘图实现
│       ├── history.py           多轮对话历史
│       └── trace*.py            执行轨迹
│
├── tools/
│   ├── tool_registry.py         （V4.5）4 个类型化工具的声明与执行
│   ├── python_tool.py           run_stat_calc（exec 执行 pandas）
│   ├── loader.py                （V4.1）CSV/Excel/Word/图片读取
│   └── dataset_tool.py          数据集基础分析
│
├── services/
│   ├── llm_service.py           模型调用（含 Tool Calling 与 reasoning_content 处理）
│   ├── store.py                 （V4.15）对话持久化（一个会话一个 JSON 文件）
│   └── session_store.py         会话与数据集记忆（内存缓存 + 落库）
│
├── knowledge/
│   ├── metrics.yaml             通用业务口径（9 条）
│   └── ecommerce.yaml           （V4.13）电商经营分析口径（24 条）
│
├── data/
│   ├── online_retail.csv        （V4.13）真实数据集采样，保留全部陷阱
│   ├── online_retail_small.csv  测试用的小采样
│   └── conversations/           对话记录（gitignore）
│
├── scripts/
│   └── fetch_demo_data.py       （V4.13）下载并分层采样真实数据集
│
├── frontend/                    index.html / app.js / style.css
│
├── eval/                        评测体系
│   ├── cases.yaml               12 条用例
│   ├── run_eval.py              评测运行器
│   ├── datasets/                固定测试数据
│   └── results/                 运行结果（基线对比用）
│
└── tests/                       454 个离线用例（pytest）
```

## 数据体检（V4.11）

**这是整个项目最核心的一层。**

通用 Agent 会给你一个**自信的错误结论** —— 不是算错，是结论站不住。
最典型的一种：数据往往截到某个月的中途，而模型把它当成完整月份去比较。

真实数据（UCI Online Retail，541,909 行）实测：

| | 净销售额 | 交易日 |
|---|---|---|
| 2011-11 | 1,461,756.25 | 26 |
| 2011-12 | 433,668.01 | **8** |

直接比总额 **−70.3%**，按日均比 **−3.6%**。数据里没有暴跌，只是 12 月只到 12-09。

体检在分析之前用**确定性规则**（纯 pandas，不经过模型）扫出这类问题：

| 检查 | 严重度 | 触发的条件 |
|---|---|---|
| `partial_last_period` | block | 最后一个自然月的覆盖天数明显低于此前各月 |
| `negative_values` | block | 数值列有负值（退货/冲销），合计因此有两种口径 |
| `missing_key_dimension` | warn | 客户/用户类字段缺失超过 5% |
| `non_positive_price` | warn | 价格/金额列有 ≤ 0 的值（赠品、调整项） |
| `dimension_concentration` | warn | 某个维度取值集中度超过 80% |
| `empty_or_constant_column` | warn / info | 整列为空或只有一个取值 |

**为什么必须是确定性的**：这些结论会被用来**拦截报告里的错误结论**。
如果体检本身是模型判断的，那拦截就成了拿一个猜测去校验另一个猜测。

发现项有三个去处：

1. 注入 Planner / Executor 的提示词 —— 算之前就知道有什么坑
2. 注入 Reporter 的提示词 —— 必须如实披露
3. 由**程序**在报告末尾追加「数据限制说明」—— 不会被模型漏掉

### 两个只有用真实数据才会暴露的坑

**日期列是字符串。** 项目里的数据一律会被归一化成 CSV，
所以日期进来时是 `"2011-12-01 08:26:00"` 而不是 datetime 类型。
如果只认 `datetime64`，「期间不完整」这个最重要的检查**永远不会触发**。

**不完整的期间只在数据尾部。** 数据按时间排序，最后一个不完整的月份就在文件末尾。
如果体检只看前 N 行（我最初设了 20 万行上限），最重要的发现正好被截掉。

## 结论核对（V4.12）

数值核对管「报告里的数字对不对」，这一层管「**报告的结论，这份数据撑不撑得住**」。

它把体检里 `severity = block` 的发现项逐条变成可程序检查的规则：

| 规则 | 什么时候算违规 |
|---|---|
| 期间不可比 | 报告拿了那个不完整期间做比较，**且自己没有声明这个限制** |
| 口径未声明 | 数据里有负值，报告给了总额却没说是毛额还是净额 |
| 覆盖率未声明 | 报告做了客户维度分析，却没提客户 ID 缺失了多少 |

**设计上刻意保守**：一句话只有在「做了比较」且「没有自己声明限制」时才算违规。

否则它会去指责报告里那句本来正确的披露 ——
「12 月数据不完整，不可比」是**正确**的，不该被拦。

Markdown 表格行会继承表头的字样（`| 月份 | 环比 |` 下的数据行也算比较），
因为表格里放一个不可比的百分比，恰恰是违规最常见的表现形式。

命中之后由**程序**在报告末尾追加纠正说明，并在轨迹里标黄。

## 业务口径库（V4.6 / V4.13）

模型只能看到列名，不知道「净销售额」要扣退款和折扣，会拿相近字段凑一个数 ——
**结果看起来正常，但算的不是那个东西**。这是数据分析里最隐蔽的一类错误。

知识库目录下可以有多个领域包：

| 文件 | 内容 |
|---|---|
| `metrics.yaml` | 通用口径 9 条（净销售额、客单价、毛利率…） |
| `ecommerce.yaml` | 电商经营分析 24 条（净销售额、复购率、退货率、期间可比性…） |

每条包含 `name / aliases / definition / formula / requires / keywords / note`。

### 按字段适用性筛选

同一句问题在不同数据集上要注入**不同**的口径：

| 数据集 | 命中的「净销售额」 | 跳过的 |
|---|---|---|
| `demo_orders`（有退款/折扣列） | `requires = [销售额, 退款金额, 折扣金额]` | 25 条 |
| `online_retail`（只有 Quantity/UnitPrice） | `requires = [Quantity, UnitPrice]` | 9 条 |

**同名条目刻意不去重** —— 同一个指标在不同领域里的口径本来就不同，
硬去重会随文件读取顺序随机丢一个。交给适用性筛选。

### 一个例外：问题里点名的口径即使缺字段也要注入

「毛利率」需要「成本」列，而演示数据里没有。
如果把这条筛掉，模型就会去拿相近字段凑利润率 ——
**而这条口径存在的意义恰恰是告诉它「你缺成本，不要硬算」。**

所以问题里明确点名的口径会被保留，并在注入时标注缺了哪些字段。

### 检索方式：字符二元组 + 别名匹配，加 LLM 兜底

**不用向量检索**：

- 当前 API 供应商没有 embedding 接口（实测所有候选模型都是 404）
- 字典只有几十条，问题里通常直接出现指标名
- 中文没有空格，但二元组重叠不需要分词就能算
- 不需要新依赖，没有每次查询的 embedding 费用，命中理由可解释

只有词法完全没命中时才让模型从字典里挑一次，处理「实际收入」→「净销售额」这类词汇鸿沟。

## 数值核对（V4.4）

`verify` 节点把报告里的数值和 `execution_results` 里真实存在的数值逐个核对：

1. 抽出报告里的数值（处理千分位、万/亿、百分号，屏蔽日期和周期标签）
2. 从执行结果里抽出真实数值
3. 把合理派生也算作合法来源：组内求和/均值、两两之和差、比值、**子集之和**、**百分比反推分子**
4. 追溯不到的由**程序**追加提示到报告末尾，并在轨迹里标记

关键设计：**容差分两档**。原始数值允许 1% 相对误差（报告会写「约 17.5 万」）；
子集求和是精确算术，绝对误差上限 10，算错了必须被抓出来。

## 原生 Tool Calling（V4.5）

Executor 不是「让模型写代码然后 exec」，而是真的用 tools 协议：

| 工具 | 参数 | 用途 |
|---|---|---|
| `describe_dataset` | — | 看字段、类型、缺失值 |
| `aggregate_metric` | `group_by`（可为数组）/ `metric` / `agg` / `time_granularity` | 分组聚合，**主力工具** |
| `describe_metric` | `metric` | 分布统计 |
| `run_python` | `code` | 逃生通道，专用工具覆盖不到时才用 |

流程：模型返回结构化 `tool_calls` → 执行对应函数 → 结果作为 `role="tool"` 回填 →
循环直到模型不再调用工具（最多 8 轮）。

**踩过的两个坑**（模型跑在思考模式下）：

1. `tool_choice` 不能强制指定，会返回 400，所以固定用 `auto`
2. 回填历史时必须带上 `reasoning_content`，且**每一个 `tool_call_id` 都必须有对应回复**，
   少一个也会 400

收益：常规分析不经过 `exec`，攻击面显著缩小；每次调用都有名字和参数，可记录可审查。

## 对话持久化（V4.14 / V4.15）

之前会话只在内存里，重启服务对话就没了 —— 那是个演示页，不是一个应用。

存法是**一个会话一个 JSON 文件**：

```
data/conversations/<session_id>.json
```

文件内容就是会话本身（数据绑定、每一轮问答、报告正文），没有派生字段 ——
打开文件就能看懂这次分析发生了什么。

**为什么用文件而不是数据库**：能直接读、diff、grep、备份，不需要任何工具；
没有 schema，没有迁移；单机单用户没有并发写入问题。
代价是列表要读目录下所有文件（摘要字段就在各自文件里），
本地几百个会话是毫秒级，真要涨上去再加索引。

写入是**原子**的：先写 `.tmp` 再 `os.replace`，中途崩了不会留下半个 JSON 文件。

### 存储不可用时必须降级

实测启动服务时撞到：存储目录建不出来（只读安装、磁盘满、权限不足），
`GET /conversations` 直接 500。

**真正要命的不是列表接口**：`session_store.create()` 会调 `save_conversation`，
所以存储出问题会**把分析本身一起拖垮**。

历史记录只是便利功能，绝不该有这种后果。所以所有存储操作都静默降级：
读返回空，写直接丢弃，分析链路完全不受影响。

## 输入格式（V4.1）

| 格式 | 处理方式 |
|---|---|
| CSV / Excel | 直接读取（编码自动尝试 utf-8/utf-8-sig/gbk/latin-1；Excel 自动选最佳工作表） |
| Word | 提取表格 |
| 图片 | 视觉模型提取表格 |

所有格式最终归一化成 CSV，所以分析链路只认一个 CSV 路径。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 前端页面 |
| `POST /analyze` | 一次性返回完整结果 |
| `POST /chat/stream` | SSE 流式，逐节点推送执行过程 |
| `GET /conversations` | 历史对话列表 |
| `GET /conversations/{id}` | 读取一条对话（供界面渲染历史） |
| `POST /conversations/{id}/rename` | 改名 |
| `DELETE /conversations/{id}` | 删除 |
| `GET /conversations/{id}/export` | 导出 Markdown |
| `/temp` `/charts` `/frontend` | 静态资源 |

SSE 事件类型：`start` / `trace` / `charts` / `report` / `meta` / `error` / `done`。

## 评测体系（V4.7）

```bash
python eval/run_eval.py --check              # 离线校验用例，不花钱
python eval/run_eval.py                      # 全量（12 条，约 6 分钟）
python eval/run_eval.py --tag knowledge      # 按标签
python eval/run_eval.py --compare            # 与基线对比
```

**判定原则：只看 Agent 真实算出来的数值，不让模型读报告打分。**

「听起来对」和「算得对」是两件事。让模型判模型，一份自信的错误报告也会得高分。
用例的答案键由 pandas 独立复算校验，12 条覆盖
聚合 / 时间 / 业务口径 / 健壮性 / 工具选择 / 图表。

## 工程约定

- 源文件 UTF-8，CRLF 换行
- 变更以 `V4.x` 注释标记在代码里
- 每项能力都有对应的离线验证；改动后必须回归

## 已知限制

| 限制 | 说明 |
|---|---|
| `run_python` 无沙箱 | 仍是 `exec`；常规分析已不走它，但没有进程级隔离 |
| 采样数据不能做跨期对比 | `data/online_retail.csv` 完整保留了 2011-12，但 2011-11 是抽样的。体检按**覆盖天数**判断所以不受影响，但 11 月 vs 12 月的环比要用全量数据 |
| 历史只保留问答与报告 | 执行轨迹和图表文件不进记录，打开旧对话不会重放过程 |
| 无账号体系 | 本地单用户，没有登录与权限 |
| 报告不含图表链接 | 图表由前端独立面板展示，报告正文不插图 |
| 散文约数会触发核对提示 | 「约 2.3 万」这类模糊表达无法自动追溯，属预期行为 |
| 评测需真实模型调用 | 全量 12 条约 70 次调用，不适合放进 CI |

## 测试体系

```bash
pytest              # 454 个离线用例，约 17 秒，不调用模型
pytest -m live      # 需要真实模型的用例（会产生费用）
```

**离线是被强制的**：`tests/conftest.py` 里有一个 autouse 夹具会把 OpenAI 客户端的
`create` 换成抛异常的桩，任何漏网的真实调用都会立刻失败，而不是偷偷花钱。

另一个 autouse 夹具把对话存储目录指到临时目录 ——
测试不该污染仓库里的 `data/conversations/`。

| 文件 | 覆盖 |
|---|---|
| `test_health_check.py` | 数据体检（6 类检查 + 真实数据集断言 + 两个历史 bug 的回归） |
| `test_conclusion_check.py` | 结论核对（含「正确披露不该被拦」） |
| `test_store.py` | 对话持久化（含存储不可用时的降级） |
| `test_conversations_api.py` | 5 个会话接口（TestClient 走真实路由） |
| `test_verifier.py` | 报告数值核对（含两次历史误判的回归） |
| `test_tool_calling.py` | 工具注册表 + 工具调用循环协议 |
| `test_knowledge.py` | 业务口径检索与提示词注入 |
| `test_knowledge_packs.py` | 多领域包加载、适用性筛选、缺字段标注 |
| `test_graph_and_reviewer.py` | 图接线 + Reviewer 的空操作修复 |
| `test_temp_cleanup.py` | 临时文件清理 |
| `test_api.py` | `/analyze` 与 `/chat/stream` 的接口形状 |
| `test_time_granularity.py` | 时间粒度识别、降级与优先级 |
| `test_chart_chain.py` | 绘图链路：单图/多图/部分失败/失败披露 |
| `test_loader_formats.py` | CSV/Excel/Word/图片 统一归一化 |
| `test_session_and_streaming.py` | 会话记忆、SSE 帧、多轮追问、容量与 TTL |
| `test_eval_harness.py` | 评测体系自身的判定/指标/基线 |
| `test_chart_executor_legacy.py` | 由早期独立脚本转换而来的绘图用例 |

早期那 5 个独立脚本（用 `sys.exit` 返回、失败也退出码 0）已经全部转成 pytest。
