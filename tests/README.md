# tests

离线测试套件。**不调用真实模型**，因此可以放心在 CI 里跑。

```bash
pytest                 # 全部离线用例（默认跳过真机用例）
pytest -m live         # 需要真实模型的用例（会产生费用）
pytest tests/test_verifier.py -v
```

## 为什么测试里不可能偷偷花钱

`conftest.py` 里有一个 autouse 夹具 `no_real_llm`，它把 OpenAI 客户端的
`create` 换成会抛异常的桩。任何**漏网的真实调用都会立刻失败**，
而不是悄悄产生账单。需要真机的用例必须显式标 `@pytest.mark.live`。

## 约定

- 写文件一律走 `tmp_path`，不要往项目目录里写
  （`isolated_temp` 夹具已经把临时目录常量重定向过去了）
- 不读 `.env`：`conftest.py` 会先设好占位 key 再导入项目模块
- 用例按**被测主题**组织，不要按开发版本命名

## 目录

| 文件 | 覆盖 |
|---|---|
| `conftest.py` | 共享夹具与离线保险 |
| `test_verifier.py` | 报告数值核对（含两次历史误判的回归） |
| `test_tool_calling.py` | 工具注册表 + 工具调用循环协议 |
| `test_knowledge.py` | 业务口径检索与提示词注入 |
| `test_graph_and_reviewer.py` | 图接线 + Reviewer 的空操作修复 |
| `test_temp_cleanup.py` | 临时文件清理 |
| `test_api.py` | `/analyze` 与 `/chat/stream` 的接口形状 |
| `test_chart_executor_legacy.py` | 绘图链路（由早期的独立脚本转换而来） |
| `test_eval_harness.py` | 评测体系自身的判定/指标/基线 |
| `test_time_granularity.py` | 时间粒度识别、降级与优先级 |
| `test_chart_chain.py` | 绘图链路：单图/多图/部分失败/失败披露 |
| `test_loader_formats.py` | CSV/Excel/Word/图片 统一归一化 |
| `test_session_and_streaming.py` | 会话记忆、SSE 帧、多轮追问 |
| `test_chart_planner_live.py` | 图表规划真机用例（默认跳过） |

端到端评测（需要真实模型）不在这里，在 `eval/`：

```bash
python eval/run_eval.py --check     # 离线校验用例
python eval/run_eval.py             # 全量评测
```
