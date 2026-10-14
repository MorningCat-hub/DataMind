# DataMind

> 一个会**主动声明数据局限、并拒绝给出站不住脚结论**的数据分析 Agent。

上传一个数据文件，用中文提一个问题，得到一份**数值可核对、结论有依据**的分析报告。

---

## 它解决什么问题

通用分析 Agent 的麻烦不是算错，而是**算对了，却得出错误结论**。

真实例子（UCI Online Retail，541,909 行，全部数字实测）：

| | 净销售额 | 交易日 |
|---|---|---|
| 2011-11 | 1,461,756.25 | 26 |
| 2011-12 | 433,668.01 | **8** |

直接比总额：**−70.3%**。看起来像「12 月崩了」。

按日均比：**−3.6%**。

**数据里根本没有暴跌 —— 只是 12 月的数据只到 12-09。**
一份自信的报告会告诉你「12 月销售额暴跌 70%」，而它引用的每一个数字都是真的。

这类错误通用 Agent 结构上就避不开：**它不知道 12 月是短的。**

DataMind 的做法是，在分析之前先用**确定性规则**把数据的局限查清楚，
再拿这些局限去**拦截报告里站不住的结论**。

---

## 四层防线

| 层 | 做什么 | 为什么可信 |
|---|---|---|
| **数据体检** | 分析前查出 6 类问题：期间不完整、负值（退货/冲销）、关键维度缺失、单价异常、维度过度集中、空列/常量列 | **纯 pandas，不经过模型** |
| **业务口径库** | 33 条领域口径，按数据集**字段适用性**注入；缺字段的口径会被标注「无法计算，不要凑」 | 每条的数字都在真实数据上实测 |
| **三层自愈** | 工具报错 → 模型改参数重试；任务失败 → 生成修复建议重跑；结果不完备 → 重新规划 | 每层都有明确上限 |
| **结论核对** | 报告结论与数据限制冲突时，由**程序**追加纠正说明 | 规则直接来自体检结论 |

**为什么这些检查必须是确定性的**：

它们要用来**拦截报告的结论**。如果检查本身是模型判断的，
那就是拿一个猜测去校验另一个猜测 —— 拦截也就失去意义了。

### 体检在真实数据上的实际输出

```
[block] 最后一个期间不完整（2011-12 只覆盖 9 天，25,525 行，典型 30 天）
[block] 「Quantity」存在负值（10,624 行 = 2.0%，其中 9,288 行 C 开头发票）
[warn]  「CustomerID」缺失 24.9%（135,080 行）
[warn]  「UnitPrice」有 2,517 行 <= 0
[warn]  「Country」高度集中（United Kingdom 91.4%）
```

耗时 **0.9 秒 / 541,909 行**，全程没有模型参与。

### 结论核对的实际拦截

一份写着「2011-12 销售额环比下降 70%」的报告会被拦下：

```markdown
## ⚠️ 结论核对提示

- **2011-12 只覆盖 9 天，不是完整期间（典型 30 天）。把它与完整期间做比较，结论不成立。**
  - 报告原文：`2011-12 销售额环比下降 70%，是全年最大的跌幅`
  - 建议：改用日均口径，或把 2011-12 排除在比较范围之外

这段提示由程序生成，不经过模型，所以它一定和体检结果一致。
```

而一份**自己声明了限制**的报告（「12 月仅覆盖 9 天，不宜与完整月份直接比较」）
会正常通过 —— 检查是保守的，不会去指责报告里那句本来正确的披露。

---

## 架构

```
                        文件（CSV / Excel / Word / 图片）
                                    │
                        ┌───────────▼───────────┐
                        │  dataset              │  多格式读取 → 统一 CSV
                        └───────────┬───────────┘
                                    │
                        ┌───────────▼───────────┐
                        │  health_check   V4.11 │  数据体检（确定性，纯 pandas）
                        └───────────┬───────────┘  期间不完整 / 负值 / 维度缺失 …
                                    │
                        ┌───────────▼───────────┐
                        │  retrieve        V4.6 │  按字段适用性检索业务口径
                        └───────────┬───────────┘
                                    │
                        ┌───────────▼───────────┐
                        │  planner              │  拆解成可独立执行的任务
                        └───────────┬───────────┘
                                    │
                        ┌───────────▼───────────┐
              ┌────────►│  executor        V4.5 │  原生 Tool Calling 循环
              │         └───────────┬───────────┘
              │                     │
              │         ┌───────────▼───────────┐
              │         │  error_handler        │  失败 → 生成修复建议
              │         └───────────┬───────────┘
              │                     │
              └─────────────────────┤  最多 8 轮工具调用 / 每任务 2 次修复
                                    │
                        ┌───────────▼───────────┐
                        │  reviewer             │  审查完备性（最多 2 次全局重规划）
                        └───────────┬───────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
        ┌───────────▼───────────┐       ┌───────────▼───────────┐
        │  chart_planner        │       │  report               │
        │  chart_executor       │       │  生成最终报告          │
        └───────────────────────┘       └───────────┬───────────┘
                                                    │
                                        ┌───────────▼───────────┐
                                        │  verify           V4.4│  数值可追溯性核对
                                        └───────────┬───────────┘
                                                    │
                                        ┌───────────▼───────────┐
                                        │  conclusion_check V4.12│ 结论级核对
                                        └───────────────────────┘
```

**14 个 LangGraph 节点，`AgentState` 是唯一的数据总线。**

### 三层自愈

| 层级 | 机制 | 上限 |
|---|---|---|
| 任务内 | 工具报错回填给模型，模型自己改参数 | 单任务 8 轮工具调用 |
| 任务级 | `error_handler` 生成修复建议后重跑该任务 | 每任务 2 次 |
| 全局 | `reviewer` 判定不完备 → `retry` → 重新规划 | 2 次 |

### 为什么用原生 Tool Calling 而不是「让模型写代码」

Executor 真的走 tools 协议，四个类型化工具：

| 工具 | 用途 |
|---|---|
| `describe_dataset` | 看字段、类型、缺失值 |
| `aggregate_metric` | 分组聚合（支持多列 group_by、时间粒度），**主力工具** |
| `describe_metric` | 分布统计 |
| `run_python` | 逃生通道，专用工具覆盖不到时才用 |

收益：常规分析不经过 `exec`，攻击面显著缩小；每次调用都有名字和参数，**可记录、可审查**。

---

## 快速开始

```bash
git clone <repo>
cd datamind-v4.0

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

# 配置模型
copy .env.example .env            # 然后把里面的 key 换成你自己的
```

`.env`：

```
OPENAI_API_KEY=sk-你的key
OPENAI_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
```

启动：

```bash
uvicorn main:app --reload
# 打开 http://127.0.0.1:8000
```

### 试一下

仓库里已经放好了一份**真实数据集**（UCI Online Retail 的分层采样，保留了全部数据陷阱）：

```
data/online_retail.csv         100,000 行
data/online_retail_small.csv    17,769 行（测试用，跑得快）
```

上传 `data/online_retail.csv`，问：

> 2011 年 11 月到 12 月的销售额变化是多少？为什么会变化？

你会看到体检先报出「2011-12 只覆盖 9 天」，
然后报告在给结论时必须处理这个限制。

---

## 项目结构

```
datamind-v4.0/
├── main.py                      FastAPI 入口、SSE 流式接口、会话 API
├── ARCHITECTURE.md              详细架构说明
│
├── agent/
│   ├── state.py                 AgentState（唯一数据总线）
│   ├── graph.py                 StateGraph 组装 + 路由
│   ├── nodes/                   14 个节点
│   └── utils/
│       ├── health_check.py      数据体检（确定性，6 类检查）
│       ├── conclusion_check.py  结论级核对（3 条规则）
│       ├── verifier.py          报告数值核对
│       ├── knowledge.py         口径检索（二元组 + 别名，含 LLM 兜底）
│       └── time_granularity.py  时间粒度识别与降级
│
├── tools/
│   ├── tool_registry.py         4 个类型化工具的声明与执行
│   ├── loader.py                CSV / Excel / Word / 图片读取
│   └── python_tool.py           run_python 逃生通道
│
├── services/
│   ├── llm_service.py           模型调用（Tool Calling + reasoning_content 处理）
│   ├── store.py                 对话持久化（一个会话一个 JSON 文件）
│   └── session_store.py         会话与数据集记忆
│
├── knowledge/
│   ├── metrics.yaml             通用口径（9 条）
│   └── ecommerce.yaml           电商经营分析口径（24 条，数字全部实测）
│
├── data/
│   ├── online_retail.csv        真实数据集采样（保留全部陷阱）
│   └── conversations/           你的对话记录（已 gitignore）
│
├── scripts/
│   └── fetch_demo_data.py       下载并分层采样真实数据集
│
├── frontend/                    index.html / app.js / style.css
├── eval/                        评测体系（12 条用例 + 基线）
└── tests/                       454 个离线用例
```

---

## 测试

```bash
pytest                          # 454 个用例，约 17 秒，不调用模型，不花钱

# 需要真实模型的用例：两个开关都要给（会产生费用）
set DATAMIND_LIVE=1 && pytest -m live
```

**离线是被强制的**：`tests/conftest.py` 里有一个 autouse 夹具把 OpenAI 客户端的
`create` 换成抛异常的桩 —— 任何漏网的真实调用都会**立刻失败**，而不是偷偷产生费用。

覆盖范围包括：报告数值核对（含历史误判的回归）、工具调用协议、口径检索、
数据体检（含真实数据集上的断言）、结论核对、图接线、时间粒度、
绘图链路、多格式读取、会话持久化与降级、评测体系自身。

## 评测

```bash
python eval/run_eval.py --check     # 离线校验用例与知识库，不花钱
python eval/run_eval.py             # 全量 12 条，需真实模型调用
python eval/run_eval.py --compare   # 与基线对比
```

**判定原则：只看 Agent 真实算出来的数值，不让模型读报告打分。**

「听起来对」和「算得对」是两件事。让模型判模型，一份自信的错误报告也会得高分。
用例的答案键由 pandas 独立复算校验。

---

## 已知限制

诚实清单，不是待办事项的美化版：

| 限制 | 说明 |
|---|---|
| `run_python` 无沙箱 | 仍是 `exec`；常规分析已不走它，但没有进程级隔离 |
| 采样数据不能做跨期对比 | `data/online_retail.csv` 完整保留了 2011-12，但 2011-11 是抽样的。**体检能正确工作**（按覆盖天数判断），但 11 月 vs 12 月的环比要用全量数据（`scripts/fetch_demo_data.py --full`） |
| 历史只保留问答与报告 | 执行轨迹和图表文件不进记录，所以打开旧对话不会重放过程，旧图表可能已被清理 |
| 无账号体系 | 本地单用户，没有登录与权限 |
| 报告不内嵌图表 | 图表由前端独立面板展示 |
| 散文约数会触发核对提示 | 「约 2.3 万」这类模糊表达无法自动追溯，属预期行为 |
| 评测需真实模型调用 | 全量 12 条约 70 次调用，不适合放进 CI |

---

## 数据集来源

演示数据来自 **UCI Machine Learning Repository** 的 Online Retail 数据集：

- Chen, D. (2015). *Online Retail* [Dataset]. UCI Machine Learning Repository.
  https://doi.org/10.24432/C5BW33
- 许可：**CC BY 4.0**（署名必需）— https://creativecommons.org/licenses/by/4.0/
- 论文：Chen, Sain & Guo (2012), *Journal of Database Marketing & Customer
  Strategy Management* 19(3), 197–208.

仓库里的 `data/online_retail*.csv` 是按分层策略采样得到的子集，
**故意保留**了原数据的全部质量问题（负数量、缺失客户 ID、零单价、
不完整的最后一个月份等），因为这些正是本项目要处理的对象。

> 再分发前请自行确认隐私与许可立场：UCI 未说明 `CustomerID` 是否已匿名化。

重新生成采样：

```bash
# 从 UCI 下载原始 xlsx 再采样（注意默认输出是 data/processed/）
python scripts/fetch_demo_data.py --full --out data/online_retail.csv

# 用本地已有的 xlsx，不走网络
python scripts/fetch_demo_data.py --source D:\path\to\Online Retail.xlsx \
    --out data/online_retail.csv
```

---

## 技术栈

Python 3.12 · FastAPI · LangGraph · pandas · OpenAI SDK（兼容 DeepSeek）· 原生 JS

## 许可

MIT，见 [LICENSE](LICENSE)。（第三方数据集许可见上节）
