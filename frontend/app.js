/*
=========================================
DataMind V4.3

聊天界面（参考 DeepSeek 布局）

要点：

1. 执行过程折叠成一行摘要。
   跑的时候只显示「当前那一步」，点开才看全部，
   不再一股脑铺一屏。

2. 数据文件是一个附件：
   常驻在输入框上方的附件条里，可以随时删除或更换。

3. SSE 流式接收与 V4.2 相同：
   服务端每跑完一个节点就推一帧。
=========================================
*/


// ========================================
// 页面元素
// ========================================

const fileInput =
    document.getElementById("file");

const attachButton =
    document.getElementById("attachButton");

const questionInput =
    document.getElementById("question");

const sendButton =
    document.getElementById("sendButton");

const newChatButton =
    document.getElementById("newChatButton");

const emptyState =
    document.getElementById("emptyState");

const messageList =
    document.getElementById("messageList");

const attachmentBar =
    document.getElementById("attachmentBar");

const composerHint =
    document.getElementById("composerHint");

const datasetBadge =
    document.getElementById("datasetBadge");

const errorSection =
    document.getElementById("errorSection");

const errorContainer =
    document.getElementById("errorContainer");


// ========================================
// 图标
// ========================================

const ICON_MAP = {

    code: "⌨",

    calculator: "▣",

    alert: "!",

    search: "⌕",

    wrench: "⚙",

    check: "✓",

    info: "•"
};


const CHART_TYPE_LABELS = {

    bar: "柱状图",

    horizontal_bar: "水平柱状图",

    line: "折线图",

    pie: "饼图",

    histogram: "直方图",

    scatter: "散点图"
};


const SOURCE_TYPE_LABELS = {

    csv: "CSV 文件",

    excel: "Excel 文件",

    word_table: "Word 文档表格",

    word_text: "Word 文档正文",

    image_table: "图片（视觉识别）",

    image_text: "图片内容"
};


// ========================================
// 状态
// ========================================

let sessionId = "";

let streaming = false;

let abortController = null;

// 还没发送的附件
let pendingFile = null;

// 当前会话已经加载好的数据集文件名
let activeFileName = "";


// ========================================
// 事件绑定
// ========================================

sendButton.addEventListener(
    "click",
    handleSendClick
);

newChatButton.addEventListener(
    "click",
    startNewChat
);

attachButton.addEventListener(
    "click",
    openFilePicker
);

fileInput.addEventListener(
    "change",
    onFileChosen
);

questionInput.addEventListener(
    "input",
    autoGrow
);

questionInput.addEventListener(
    "keydown",
    function (event) {

        if (
            event.key === "Enter" &&
            !event.shiftKey
        ) {

            event.preventDefault();

            handleSendClick();
        }
    }
);


// ========================================
// 附件
// ========================================

function openFilePicker() {

    fileInput.click();
}


function onFileChosen() {

    const file = fileInput.files[0];

    if (!file) {

        return;
    }

    pendingFile = file;

    renderAttachment();

    updateComposerHint();
}


function setPendingFile(file) {

    pendingFile = file || null;

    renderAttachment();

    updateComposerHint();
}


function clearPendingFile() {

    pendingFile = null;

    fileInput.value = "";

    renderAttachment();

    updateComposerHint();
}


function onAttachmentRemove() {

    // 还没发送 -> 只是取消这次的选择
    if (pendingFile) {

        clearPendingFile();

        return;
    }

    // 已经加载的数据集 -> 移除意味着开始一次全新的分析
    if (activeFileName) {

        startNewChat();
    }
}


function renderAttachment() {

    const name =
        pendingFile
            ? pendingFile.name
            : activeFileName;

    attachmentBar.innerHTML = "";

    if (!name) {

        attachmentBar.classList.add("hidden");

        return;
    }

    const chip =
        document.createElement("div");

    chip.className = pendingFile
        ? "attachment-chip attachment-pending"
        : "attachment-chip";

    const icon =
        document.createElement("span");

    icon.className = "attachment-icon";

    icon.textContent = pendingFile ? "📎" : "📄";

    chip.appendChild(icon);

    const label =
        document.createElement("span");

    label.className = "attachment-name";

    label.textContent = name;

    chip.appendChild(label);

    const remove =
        document.createElement("button");

    remove.type = "button";

    remove.className = "attachment-remove";

    remove.textContent = "×";

    remove.title = pendingFile
        ? "移除附件"
        : "移除并开始新对话";

    remove.addEventListener(
        "click",
        onAttachmentRemove
    );

    chip.appendChild(remove);

    attachmentBar.appendChild(chip);

    attachmentBar.classList.remove("hidden");
}


function updateComposerHint() {

    if (pendingFile) {

        composerHint.textContent =
            "将上传：" + pendingFile.name;

        return;
    }

    if (activeFileName) {

        composerHint.textContent =
            "已加载 " + activeFileName
            + "，可以直接追问；点 📎 可以换一个文件";

        return;
    }

    composerHint.textContent =
        "支持 CSV / Excel / Word / 图片；第一轮请先添加附件";
}


function autoGrow() {

    questionInput.style.height = "auto";

    questionInput.style.height =
        Math.min(questionInput.scrollHeight, 160) + "px";
}


// ========================================
// 新建对话
// ========================================

function startNewChat() {

    if (streaming && abortController) {

        abortController.abort();
    }

    sessionId = "";

    activeFileName = "";

    pendingFile = null;

    fileInput.value = "";

    messageList.innerHTML = "";

    emptyState.classList.remove("hidden");

    errorSection.classList.add("hidden");

    datasetBadge.classList.add("hidden");

    renderAttachment();

    updateComposerHint();
}


// ========================================
// 发送 / 停止
// ========================================

function handleSendClick() {

    if (streaming) {

        if (abortController) {

            abortController.abort();
        }

        return;
    }

    send();
}


async function send() {

    if (streaming) {

        return;
    }

    const question =
        questionInput.value.trim();

    const file = pendingFile;

    if (!question) {

        alert("请输入分析问题");

        return;
    }

    if (!sessionId && !file) {

        alert("第一轮请先添加数据文件");

        return;
    }

    errorSection.classList.add("hidden");

    questionInput.value = "";

    autoGrow();

    // 换了文件 = 新的分析上下文
    if (file && sessionId) {

        messageList.innerHTML = "";
    }

    emptyState.classList.add("hidden");

    appendUserMessage(
        question,
        file ? file.name : (activeFileName || "")
    );

    const assistant =
        createAssistantMessage();

    // 附件已经交给助手消息展示，
    // 清掉输入区的待发送状态
    if (file) {

        pendingFile = null;

        fileInput.value = "";

        renderAttachment();
    }

    scrollToBottom();

    streaming = true;

    sendButton.disabled = true;

    sendButton.textContent = "■";

    abortController = new AbortController();

    const formData = new FormData();

    formData.append("question", question);

    formData.append("session_id", sessionId);

    if (file) {

        formData.append("file", file);
    }

    try {

        const response = await fetch(
            "/chat/stream",
            {
                method: "POST",
                body: formData,
                signal: abortController.signal
            }
        );

        if (!response.ok) {

            throw new Error(
                "服务器请求失败：HTTP " + response.status
            );
        }

        await consumeStream(
            response,
            assistant
        );

    } catch (error) {

        if (error.name === "AbortError") {

            setAssistantStatus(
                assistant,
                "已停止",
                "warning"
            );

        } else {

            setAssistantError(
                assistant,
                "请求失败：" + error.message
            );

            // 上传失败时把附件还给用户，方便重试
            if (file && !activeFileName) {

                setPendingFile(file);
            }
        }

    } finally {

        streaming = false;

        abortController = null;

        sendButton.disabled = false;

        sendButton.textContent = "↑";

        updateComposerHint();

        scrollToBottom();
    }
}


// ========================================
// 消费 SSE
// ========================================

async function consumeStream(
    response,
    assistant
) {

    const reader =
        response.body.getReader();

    const decoder =
        new TextDecoder("utf-8");

    let buffer = "";

    while (true) {

        const chunk =
            await reader.read();

        if (chunk.done) {

            break;
        }

        buffer += decoder.decode(
            chunk.value,
            { stream: true }
        );

        let index;

        while (
            (index = buffer.indexOf("\n\n")) !== -1
        ) {

            const frame =
                buffer.slice(0, index);

            buffer = buffer.slice(index + 2);

            handleFrame(frame, assistant);
        }
    }

    if (buffer.trim()) {

        handleFrame(buffer, assistant);
    }
}


function handleFrame(
    frame,
    assistant
) {

    const line =
        frame
            .split("\n")
            .find(
                function (item) {

                    return item.startsWith("data:");
                }
            );

    if (!line) {

        return;
    }

    let payload;

    try {

        payload = JSON.parse(line.slice(5).trim());

    } catch (error) {

        return;
    }

    handleEvent(payload, assistant);
}


function handleEvent(
    payload,
    assistant
) {

    try {

        dispatchEvent(payload, assistant);

    } catch (error) {

        setAssistantError(
            assistant,
            "前端处理事件出错：" + error.message
        );
    }
}


function dispatchEvent(
    payload,
    assistant
) {

    switch (payload.type) {

        case "start":

            if (payload.session_id) {

                sessionId = payload.session_id;
            }

            // 绝对不能在这里清空消息列表：
            // 本轮的用户消息和回复容器已经建好，
            // 清空会把正在生成的回复从文档上摘掉。
            setAssistantStatus(
                assistant,
                "正在执行",
                "running"
            );

            break;

        case "trace":

            appendTraceItem(assistant, payload.event);

            break;

        case "report":

            renderReport(assistant, payload.content);

            break;

        case "charts":

            renderCharts(assistant, payload.charts);

            break;

        case "meta":

            renderNotices(assistant, payload);

            break;

        case "done":

            onTurnDone(assistant, payload);

            // V4.15：这一轮已经落库，刷新一下历史列表
            refreshConversations();

            break;

        case "error":

            setAssistantError(
                assistant,
                payload.message || "未知错误"
            );

            break;

        default:

            break;
    }

    scrollToBottom();
}


function onTurnDone(
    assistant,
    payload
) {

    setAssistantStatus(
        assistant,
        "执行完成",
        "success"
    );

    collapseProcess(assistant);

    if (
        assistant.refs &&
        assistant.refs.reportBlock.classList.contains("hidden")
    ) {

        renderReport(
            assistant,
            "本次分析没有生成报告内容。"
        );
    }
}


// ========================================
// 消息
// ========================================

function appendUserMessage(
    question,
    fileName
) {

    const wrapper =
        document.createElement("div");

    wrapper.className = "message message-user";

    const body =
        document.createElement("div");

    body.className = "message-body";

    if (question) {

        const text =
            document.createElement("div");

        text.className = "message-text";

        text.textContent = question;

        body.appendChild(text);
    }

    if (fileName) {

        const attachment =
            document.createElement("div");

        attachment.className = "message-attachment";

        attachment.textContent = "📄 " + fileName;

        body.appendChild(attachment);
    }

    wrapper.appendChild(body);

    messageList.appendChild(wrapper);

    return wrapper;
}


function createAssistantMessage() {

    const wrapper =
        document.createElement("div");

    wrapper.className = "message message-assistant";

    const role =
        document.createElement("div");

    role.className = "message-role";

    role.textContent = "DataMind";

    wrapper.appendChild(role);

    const body =
        document.createElement("div");

    body.className = "message-body";

    // 数据来源
    const notice =
        document.createElement("div");

    notice.className = "notice-container hidden";

    body.appendChild(notice);

    // ====================================
    // 执行过程（折叠）
    // ====================================

    const processBlock =
        document.createElement("div");

    processBlock.className = "process-block";

    const processHeader =
        document.createElement("button");

    processHeader.type = "button";

    processHeader.className = "process-header";

    const chevron =
        document.createElement("span");

    chevron.className = "process-chevron";

    chevron.textContent = "▸";

    processHeader.appendChild(chevron);

    const processTitle =
        document.createElement("span");

    processTitle.className = "process-title";

    processTitle.textContent = "执行过程";

    processHeader.appendChild(processTitle);

    const processSummary =
        document.createElement("span");

    processSummary.className = "process-summary";

    processSummary.textContent = "正在连接服务器";

    processHeader.appendChild(processSummary);

    const status =
        document.createElement("span");

    status.className = "status status-running";

    status.textContent = "正在执行";

    processHeader.appendChild(status);

    processBlock.appendChild(processHeader);

    const processBody =
        document.createElement("div");

    processBody.className = "process-body hidden";

    const traceContainer =
        document.createElement("div");

    traceContainer.className = "trace-container";

    processBody.appendChild(traceContainer);

    processBlock.appendChild(processBody);

    body.appendChild(processBlock);

    processBlock.refs = {
        chevron: chevron,
        summary: processSummary,
        status: status,
        header: processHeader,
        body: processBody,
        trace: traceContainer,
        count: 0
    };

    processHeader.addEventListener(
        "click",
        function () {

            toggleProcess(processBlock);
        }
    );

    // ====================================
    // 报告
    // ====================================

    const reportBlock =
        document.createElement("div");

    reportBlock.className = "report-block hidden";

    const resultContainer =
        document.createElement("div");

    resultContainer.className = "result-container";

    reportBlock.appendChild(resultContainer);

    body.appendChild(reportBlock);

    // 业务口径（V4.6）
    const knowledgeLine =
        document.createElement("div");

    knowledgeLine.className = "knowledge-line hidden";

    body.appendChild(knowledgeLine);

    // 数值核对结果（V4.4）
    const verificationLine =
        document.createElement("div");

    verificationLine.className = "verification-line hidden";

    body.appendChild(verificationLine);

    // ====================================
    // 图表
    // ====================================

    const chartBlock =
        document.createElement("div");

    chartBlock.className = "charts-block hidden";

    const blockTitle =
        document.createElement("div");

    blockTitle.className = "block-title";

    blockTitle.textContent = "可视化结果";

    chartBlock.appendChild(blockTitle);

    const chartContainer =
        document.createElement("div");

    chartContainer.className = "chart-container";

    chartBlock.appendChild(chartContainer);

    body.appendChild(chartBlock);

    // ====================================
    // 错误
    // ====================================

    const errorBox =
        document.createElement("div");

    errorBox.className = "message-error hidden";

    body.appendChild(errorBox);

    wrapper.appendChild(body);

    messageList.appendChild(wrapper);

    wrapper.refs = {
        notice: notice,
        processBlock: processBlock,
        status: status,
        reportBlock: reportBlock,
        resultContainer: resultContainer,
        knowledgeLine: knowledgeLine,
        verificationLine: verificationLine,
        chartBlock: chartBlock,
        chartContainer: chartContainer,
        errorBox: errorBox
    };

    return wrapper;
}


// ========================================
// 执行过程的折叠 / 展开
// ========================================

function toggleProcess(processBlock) {

    const refs = processBlock.refs;

    if (!refs) {

        return false;
    }

    if (refs.body.classList.contains("hidden")) {

        refs.body.classList.remove("hidden");

        refs.chevron.textContent = "▾";

        return true;
    }

    refs.body.classList.add("hidden");

    refs.chevron.textContent = "▸";

    return false;
}


function collapseProcess(assistant) {

    const refs =
        assistant && assistant.refs;

    if (!refs || !refs.processBlock) {

        return;
    }

    const processRefs = refs.processBlock.refs;

    if (!processRefs) {

        return;
    }

    processRefs.body.classList.add("hidden");

    processRefs.chevron.textContent = "▸";

    processRefs.summary.textContent =
        "共 " + processRefs.count + " 步";
}


// ========================================
// 执行过程条目
// ========================================

function appendTraceItem(
    assistant,
    event
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const processRefs =
        assistant.refs.processBlock.refs;

    if (!processRefs) {

        return;
    }

    processRefs.count += 1;

    // 折叠状态下也要能看出「跑到哪了」
    processRefs.summary.textContent =
        (event && event.title) || "执行中";

    const item = createTraceItem(event);

    item.classList.add("trace-enter");

    processRefs.trace.appendChild(item);

    void item.offsetWidth;

    item.classList.remove("trace-enter");

    item.classList.add("trace-completed");
}


function createTraceItem(event) {

    event = event || {};

    const item =
        document.createElement("div");

    item.className =
        "trace-item trace-" + (event.level || "info");

    const icon =
        document.createElement("div");

    icon.className = "trace-icon";

    icon.textContent =
        ICON_MAP[event.icon] || ICON_MAP.info;

    item.appendChild(icon);

    const content =
        document.createElement("div");

    content.className = "trace-content";

    const titleRow =
        document.createElement("div");

    titleRow.className = "trace-title-row";

    const title =
        document.createElement("div");

    title.className = "trace-title";

    title.textContent = event.title || "未知操作";

    titleRow.appendChild(title);

    if (
        event.step !== null &&
        event.step !== undefined
    ) {

        const step =
            document.createElement("span");

        step.className = "trace-step";

        step.textContent = "第 " + event.step + " 步";

        titleRow.appendChild(step);
    }

    content.appendChild(titleRow);

    if (event.message) {

        const message =
            document.createElement("div");

        message.className = "trace-message";

        message.textContent = event.message;

        content.appendChild(message);
    }

    item.appendChild(content);

    return item;
}


// ========================================
// 报告
// ========================================

function renderReport(
    assistant,
    markdown
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const container =
        assistant.refs.resultContainer;

    if (!markdown) {

        container.innerHTML =
            "<p>本次没有生成分析报告。</p>";

    } else if (typeof marked !== "undefined") {

        container.innerHTML = marked.parse(markdown);

    } else {

        container.textContent = markdown;
    }

    assistant.refs.reportBlock.classList.remove("hidden");
}


// ========================================
// 图表
// ========================================

function renderCharts(
    assistant,
    charts
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const count = renderChartsInto(
        assistant.refs.chartContainer,
        charts
    );

    if (count > 0) {

        assistant.refs.chartBlock.classList.remove("hidden");
    }
}


function normalizeChartResults(charts) {

    if (Array.isArray(charts)) {

        return charts;
    }

    if (charts) {

        return [charts];
    }

    return [];
}


function renderChartsInto(
    container,
    charts
) {

    container.innerHTML = "";

    const items = normalizeChartResults(charts);

    items.forEach(
        function (chart) {

            if (chart && chart.success && chart.chart_url) {

                container.appendChild(createChartItem(chart));

            } else {

                container.appendChild(createChartErrorItem(chart));
            }
        }
    );

    return items.length;
}


function createChartItem(chart) {

    const figure =
        document.createElement("figure");

    figure.className = "chart-item";

    const image =
        document.createElement("img");

    image.className = "chart-image";

    image.src = chart.chart_url;

    image.alt = chart.title || "分析图表";

    image.loading = "lazy";

    figure.appendChild(image);

    const caption =
        document.createElement("figcaption");

    caption.className = "chart-caption";

    const title =
        document.createElement("div");

    title.className = "chart-title";

    title.textContent = chart.title || "分析图表";

    caption.appendChild(title);

    const typeLabel =
        CHART_TYPE_LABELS[chart.chart_type];

    if (typeLabel) {

        const meta =
            document.createElement("div");

        meta.className = "chart-meta";

        meta.textContent = typeLabel;

        caption.appendChild(meta);
    }

    if (chart.description) {

        const description =
            document.createElement("div");

        description.className = "chart-description";

        description.textContent = chart.description;

        caption.appendChild(description);
    }

    figure.appendChild(caption);

    return figure;
}


function createChartErrorItem(chart) {

    const box =
        document.createElement("div");

    box.className = "chart-item chart-item-error";

    const title =
        document.createElement("div");

    title.className = "chart-error-title";

    title.textContent = "图表生成失败";

    box.appendChild(title);

    const message =
        document.createElement("div");

    message.className = "chart-error-message";

    message.textContent = chartErrorMessage(chart);

    box.appendChild(message);

    return box;
}


function chartErrorMessage(chart) {

    if (!chart) {

        return "后端未返回图表结果。";
    }

    const error = chart.error;

    if (error && error.message) {

        return error.message;
    }

    if (!chart.success) {

        return "未知原因导致图表生成失败。";
    }

    return "图表已生成，但缺少可访问的图片地址。";
}


// ========================================
// 数据来源
// ========================================

function renderNotices(
    assistant,
    payload
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const container = assistant.refs.notice;

    const notes =
        Array.isArray(payload.dataset_notes)
            ? payload.dataset_notes.filter(Boolean)
            : [];

    const sourceLabel =
        SOURCE_TYPE_LABELS[payload.source_type];

    container.innerHTML = "";

    if (sourceLabel || notes.length) {

        if (sourceLabel) {

            const source =
                document.createElement("div");

            source.className = "notice-source";

            source.textContent = "数据来源：" + sourceLabel;

            container.appendChild(source);
        }

        notes.forEach(
            function (note) {

                const item =
                    document.createElement("div");

                item.className = "notice-item";

                item.textContent = note;

                container.appendChild(item);
            }
        );

        container.classList.remove("hidden");
    }

    // 附件条要跟着显示当前数据集的文件名
    if (payload.source_name) {

        activeFileName = payload.source_name;

        renderAttachment();
    }

    renderKnowledge(
        assistant,
        payload.knowledge_hits
    );

    renderVerification(
        assistant,
        payload.verification
    );

    setBadge(payload.source_type);
}


// ========================================
// 业务口径（V4.6）
// ========================================

function renderKnowledge(
    assistant,
    hits
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const line = assistant.refs.knowledgeLine;

    if (!line) {

        return;
    }

    if (!hits || !hits.length) {

        line.classList.add("hidden");

        return;
    }

    const names = hits.map(
        (item) => item.name
    ).join("、");

    line.classList.remove("hidden");

    line.textContent =
        "已应用业务口径：" + names;
}


// ========================================
// 数值核对结果（V4.4）
// ========================================

function renderVerification(
    assistant,
    verification
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const line = assistant.refs.verificationLine;

    if (!line) {

        return;
    }

    if (
        !verification ||
        !verification.checked
    ) {

        line.classList.add("hidden");

        return;
    }

    line.classList.remove(
        "hidden",
        "verification-ok",
        "verification-warn"
    );

    if (verification.passed) {

        line.classList.add("verification-ok");

        line.textContent =
            "✓ 数值核对：报告里 "
            + verification.checked
            + " 个数值全部可追溯到计算结果";

    } else {

        line.classList.add("verification-warn");

        line.textContent =
            "⚠ 数值核对："
            + verification.unmatched_count
            + " / "
            + verification.checked
            + " 个数值无法追溯到计算结果，已在报告末尾列出";
    }
}


function setBadge(sourceType) {

    const label = SOURCE_TYPE_LABELS[sourceType];

    if (!label) {

        datasetBadge.classList.add("hidden");

        datasetBadge.textContent = "";

        return;
    }

    datasetBadge.textContent = label;

    datasetBadge.classList.remove("hidden");
}


// ========================================
// 状态与错误
// ========================================

function setAssistantError(
    assistant,
    message
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const box = assistant.refs.errorBox;

    if (box) {

        box.textContent = message;

        box.classList.remove("hidden");
    }

    setAssistantStatus(
        assistant,
        "执行失败",
        "error"
    );

    showError(message);
}


function setAssistantStatus(
    assistant,
    text,
    level
) {

    if (!assistant || !assistant.refs) {

        return;
    }

    const status = assistant.refs.status;

    status.textContent = text;

    status.className = "status status-" + (level || "info");
}


function showError(message) {

    errorSection.classList.remove("hidden");

    errorContainer.textContent = message;
}


// ========================================
// 滚动
// ========================================

function scrollToBottom() {

    window.scrollTo({
        top: document.body.scrollHeight,
        behavior: "smooth"
    });
}


// ========================================
// 初始化
//
// 附件条和提示文案都由 JS 按当前状态渲染，
// 不依赖 index.html 里的初始 class —— 否则
// HTML 和 JS 两边状态一旦不一致就会出错。
// ========================================

renderAttachment();

updateComposerHint();


// 供离线测试使用
if (typeof globalThis !== "undefined") {

    globalThis.__api = {
        createTraceItem: createTraceItem,
        renderChartsInto: renderChartsInto,
        normalizeChartResults: normalizeChartResults,
        handleEvent: handleEvent,
        createAssistantMessage: createAssistantMessage,
        handleFrame: handleFrame,
        send: send,
        toggleProcess: toggleProcess,
        collapseProcess: collapseProcess,
        renderAttachment: renderAttachment,
        renderKnowledge: renderKnowledge,
        renderVerification: renderVerification,
        setPendingFile: setPendingFile,
        clearPendingFile: clearPendingFile,
        updateComposerHint: updateComposerHint
    };
}


// ============================================================
// V4.15 历史对话
//
// 会话记录存在服务端（data/conversations/ 下一个会话一个 JSON 文件）。
// 这里负责列出、打开、删除、导出。
//
// 说清楚范围：
// 历史只保留「问题 + 报告正文」。
// 执行过程和图表文件没有随记录保存，
// 所以打开旧对话不会重放过程，旧图表也可能已经被清理掉了。
// ============================================================

function sidebarRefs() {

    return {
        sidebar: document.getElementById("sidebar"),
        list: document.getElementById("conversationList"),
        toggle: document.getElementById("sidebarToggle"),
    };
}


function formatTime(seconds) {

    if (!seconds) {

        return "";
    }

    const date = new Date(seconds * 1000);

    const pad = (value) => String(value).padStart(2, "0");

    return (
        pad(date.getMonth() + 1)
        + "-"
        + pad(date.getDate())
        + " "
        + pad(date.getHours())
        + ":"
        + pad(date.getMinutes())
    );
}


async function refreshConversations() {

    const refs = sidebarRefs();

    if (!refs.list) {

        return;
    }

    try {

        const response = await fetch("/conversations");

        if (!response.ok) {

            return;
        }

        const data = await response.json();

        renderConversationList(data.conversations || []);

    } catch (error) {

        // 历史列表拉不到不该影响正在进行的分析
    }
}


function renderConversationList(items) {

    const refs = sidebarRefs();

    if (!refs.list) {

        return;
    }

    refs.list.innerHTML = "";

    if (!items.length) {

        const empty = document.createElement("div");

        empty.className = "sidebar-empty";

        empty.textContent = "还没有历史对话。开始一次分析就会出现在这里。";

        refs.list.appendChild(empty);

        return;
    }

    items.forEach((item) => {

        const row = document.createElement("div");

        row.className = "conversation-item";

        row.dataset.sessionId = item.session_id;

        if (item.session_id === sessionId) {

            row.classList.add("active");
        }

        const main = document.createElement("div");

        main.className = "conversation-main";

        const title = document.createElement("div");

        title.className = "conversation-title";

        title.textContent = item.title || "未命名";

        const meta = document.createElement("div");

        meta.className = "conversation-meta";

        const bits = [formatTime(item.updated_at)];

        if (item.turn) {

            bits.push(item.turn + " 轮");
        }

        if (item.source_name) {

            bits.push(item.source_name);
        }

        meta.textContent = bits.filter(Boolean).join(" · ");

        main.appendChild(title);
        main.appendChild(meta);

        const actions = document.createElement("div");

        actions.className = "conversation-actions";

        const exportButton = document.createElement("button");

        exportButton.type = "button";
        exportButton.className = "conversation-action";
        exportButton.title = "导出 Markdown";
        exportButton.textContent = "导出";

        exportButton.addEventListener("click", (event) => {

            event.stopPropagation();

            window.location.href =
                "/conversations/" + item.session_id + "/export";
        });

        const deleteButton = document.createElement("button");

        deleteButton.type = "button";
        deleteButton.className = "conversation-action";
        deleteButton.title = "删除这条对话";
        deleteButton.textContent = "删除";

        deleteButton.addEventListener("click", (event) => {

            event.stopPropagation();

            removeConversation(item.session_id);
        });

        actions.appendChild(exportButton);
        actions.appendChild(deleteButton);

        row.appendChild(main);
        row.appendChild(actions);

        row.addEventListener("click", () => {

            openConversation(item.session_id);
        });

        refs.list.appendChild(row);
    });
}


function highlightConversation(id) {

    const refs = sidebarRefs();

    if (!refs.list) {

        return;
    }

    refs.list.querySelectorAll(".conversation-item").forEach((row) => {

        row.classList.toggle("active", row.dataset.sessionId === id);
    });
}


function appendHistoryAnswer(markdown) {

    const wrapper = document.createElement("div");

    wrapper.className = "message message-assistant message-history";

    const role = document.createElement("div");

    role.className = "message-role";

    role.textContent = "DataMind";

    wrapper.appendChild(role);

    const body = document.createElement("div");

    body.className = "message-body";

    const text = document.createElement("div");

    text.className = "message-text";

    if (markdown && typeof marked !== "undefined") {

        text.innerHTML = marked.parse(markdown);

    } else {

        text.textContent = markdown || "（本次没有报告内容）";
    }

    body.appendChild(text);

    const note = document.createElement("div");

    note.className = "message-history-note";

    note.textContent =
        "历史记录只保留问题与报告正文；执行过程与图表不会重放。";

    body.appendChild(note);

    wrapper.appendChild(body);

    messageList.appendChild(wrapper);

    return wrapper;
}


async function openConversation(id) {

    if (streaming) {

        return;
    }

    let data = null;

    try {

        const response = await fetch(
            "/conversations/" + encodeURIComponent(id)
        );

        if (!response.ok) {

            return;
        }

        data = await response.json();

    } catch (error) {

        return;
    }

    // 接管这条对话：之后的追问会接到它后面
    sessionId = id;

    activeFileName = data.source_name || "";

    if (data.source_name && datasetBadge) {

        datasetBadge.textContent = data.source_name;

        datasetBadge.classList.remove("hidden");
    }

    messageList.innerHTML = "";

    if (emptyState) {

        emptyState.classList.add("hidden");
    }

    (data.history || []).forEach((turn) => {

        appendUserMessage(turn.question || "", null);

        appendHistoryAnswer(turn.answer || "");
    });

    updateComposerHint();

    highlightConversation(id);

    scrollToBottom();

    const refs = sidebarRefs();

    if (refs.sidebar) {

        refs.sidebar.classList.remove("open");
    }
}


async function removeConversation(id) {

    try {

        await fetch(
            "/conversations/" + encodeURIComponent(id),
            { method: "DELETE" }
        );

    } catch (error) {

        return;
    }

    if (id === sessionId) {

        startNewChat();
    }

    refreshConversations();
}


(function initConversationSidebar() {

    const refs = sidebarRefs();

    if (refs.toggle && refs.sidebar) {

        refs.toggle.addEventListener("click", () => {

            refs.sidebar.classList.toggle("open");
        });
    }

    if (newChatButton) {

        newChatButton.addEventListener("click", () => {

            highlightConversation("");

            const current = sidebarRefs();

            if (current.sidebar) {

                current.sidebar.classList.remove("open");
            }
        });
    }

    refreshConversations();
})();
