from typing import TypedDict, List, Dict, Any, Optional


class AgentState(TypedDict):

    # =========================
    # 用户输入
    # =========================

    question: str
    file_path: str

    # =========================
    # 数据集信息
    # =========================

    dataset_info: Optional[str]

    # =========================
    # 任务计划
    # =========================

    plan: List[Dict[str, Any]]
    current_step: int

    # =========================
    # 执行结果
    # =========================

    execution_results: List[Dict[str, Any]]

    execution_status: str
    pending_code: Optional[str]
    last_execution_error: Optional[Dict[str, Any]]
    current_task_retry_count: int
    max_task_retries: int
    code_history: List[Dict[str, Any]]

    # =========================
    # Agent Trace
    # =========================

    agent_trace: List[Dict[str, Any]]

    # =========================
    # 错误
    # =========================

    error: Optional[str]

    # =========================
    # Reviewer
    # =========================

    review_result: Optional[Dict[str, Any]]

    # =========================
    # 全局重试
    # =========================

    retry_count: int

    # =========================
    # V3.5 Chart
    # =========================

    chart_plan: Optional[Dict[str, Any]]

    # V3.9：
    # Chart Planner 最多规划 3 张图，
    # 这里保存全部执行结果 —— 生成几张就展示几张，
    # 不再出现「画了 3 张只显示 1 张」的浪费。
    chart_results: List[Dict[str, Any]]

    # =========================
    # V4.1 多格式输入
    # =========================

    # table / text / error
    input_kind: str

    # 非表格输入的内容（Word 正文、图片解读结果）
    input_context: Optional[str]

    # csv / excel / word_table / word_text / image_table / image_text
    source_type: Optional[str]

    # 给用户看的提示（例如「数据来自图片识别，可能存在误差」）
    dataset_notes: List[str]

    # =========================
    # V4.2 多轮对话
    # =========================

    # 之前的问答（[{question, answer}]）
    history: List[Dict[str, Any]]

    # 本轮是不是追问（数据集沿用上一轮）
    is_followup: bool

    session_id: Optional[str]

    # V4.3：用户原始上传的文件名（前端附件位置要显示它）
    source_name: Optional[str]

    # =========================
    # V4.4 数值核对
    # =========================

    # verify_report_numbers() 的返回值
    verification: Optional[Dict[str, Any]]

    # =========================
    # V4.6 业务口径
    # =========================

    # 检索到的口径文本，会注入 Planner 和 Executor
    knowledge_context: Optional[str]

    # 命中的口径条目（接口和前端要显示「已应用口径」）
    knowledge_hits: List[Dict[str, Any]]

    # =========================
    # V4.11 数据体检
    # =========================

    # 发现项（确定性检查的产物，会注入提示词并用于结论核对）
    health_findings: List[Dict[str, Any]]

    # 渲染好的体检文本（注入 Planner / Executor / Reporter）
    health_context: Optional[str]

    # 面向用户的一段提示（挂在报告前）
    health_notice: Optional[str]

    # =========================
    # V4.12 结论核对
    # =========================

    # 报告结论有没有和数据限制冲突
    conclusion_check: Optional[Dict[str, Any]]

    # =========================
    # 最终报告
    # =========================

    final_report: Optional[str]