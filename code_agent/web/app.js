const $ = (selector) => document.querySelector(selector);

const state = {
  sessionId: null,
  busy: false,
  startedAt: null,
  timerHandle: null,
  tools: new Map(),
  pendingApproval: null,
};

const setupLayer = $("#setup-layer");
const setupForm = $("#setup-form");
const timeline = $("#timeline");
const composer = $("#composer");
const messageInput = $("#message");
const sendButton = $("#send");

function formatTime(milliseconds) {
  if (milliseconds < 1000) return `${milliseconds}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function startTimer() {
  state.startedAt = performance.now();
  clearInterval(state.timerHandle);
  state.timerHandle = setInterval(() => {
    $("#timer").textContent = formatTime(performance.now() - state.startedAt);
  }, 100);
}

function stopTimer(elapsed) {
  clearInterval(state.timerHandle);
  $("#timer").textContent = formatTime(elapsed);
}

function scrollDown() {
  timeline.scrollTop = timeline.scrollHeight;
}

function eventShell(type, label, elapsed) {
  const section = document.createElement("section");
  section.className = `event ${type}`;
  const head = document.createElement("div");
  head.className = "event-head";
  const title = document.createElement("span");
  title.className = "event-label";
  title.textContent = label;
  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = formatTime(elapsed || 0);
  head.append(title, time);
  section.append(head);
  return section;
}

function textBody(section, text) {
  const body = document.createElement("div");
  body.className = "event-body";
  body.textContent = text;
  section.append(body);
}

function addUserMessage(text) {
  $("#welcome")?.remove();
  const section = document.createElement("section");
  section.className = "event user";
  textBody(section, text);
  timeline.append(section);
  scrollDown();
}

function approvalField(labelText, value, multiline = false) {
  const label = document.createElement("label");
  label.className = "approval-field";
  const caption = document.createElement("span");
  caption.textContent = labelText;
  const field = multiline ? document.createElement("textarea") : document.createElement("input");
  field.value = value || "";
  if (multiline) field.rows = 8;
  label.append(caption, field);
  return {label, field};
}

async function consumeEventStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) if (line.trim()) renderEvent(JSON.parse(line));
    if (done) break;
  }
  if (buffer.trim()) renderEvent(JSON.parse(buffer));
}

function renderApproval(event) {
  const section = eventShell("approval", "Human approval required", event.elapsed_ms);
  const card = document.createElement("div");
  card.className = "approval-card";
  const summary = document.createElement("div");
  summary.className = "approval-summary";
  summary.textContent = `${event.repository} · ${event.base_branch} → new branch · draft PR`;
  const branch = approvalField("New branch", event.branch);
  const commit = approvalField("Commit message", event.commit_message);
  const prTitle = approvalField("Draft PR title", event.pr_title);
  const prBody = approvalField("Draft PR body", event.pr_body, true);

  const changesTitle = document.createElement("div");
  changesTitle.className = "approval-subtitle";
  changesTitle.textContent = "Files to stage";
  const changes = document.createElement("div");
  changes.className = "approval-changes";
  for (const group of event.change_groups) {
    const row = document.createElement("label");
    row.className = "approval-check";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = true;
    input.dataset.groupId = group.id;
    const text = document.createElement("span");
    text.textContent = `[${group.status}] ${group.label}`;
    row.append(input, text);
    changes.append(row);
  }

  const diff = document.createElement("details");
  diff.className = "approval-diff";
  const diffSummary = document.createElement("summary");
  diffSummary.textContent = "Review unified diff";
  const diffText = document.createElement("pre");
  diffText.textContent = event.diff;
  diff.append(diffSummary, diffText);

  const confirmations = document.createElement("div");
  confirmations.className = "approval-confirmations";
  const confirmationLabels = [
    ["approve_branch", "Create this new local branch"],
    ["approve_stage", "Stage only the selected files"],
    ["approve_commit", "Create this local commit"],
    ["approve_push", "Push the new branch to origin"],
    ["approve_pr", "Create this draft PR through GitHub MCP"],
  ];
  for (const [name, labelText] of confirmationLabels) {
    const row = document.createElement("label");
    row.className = "approval-check confirmation";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.confirmation = name;
    const text = document.createElement("span");
    text.textContent = labelText;
    row.append(input, text);
    confirmations.append(row);
  }

  const error = document.createElement("div");
  error.className = "approval-error";
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "approval-reject";
  reject.textContent = "Cancel";
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "approval-approve";
  approve.textContent = "Approve and publish";
  actions.append(reject, approve);

  card.append(
    summary,
    branch.label,
    commit.label,
    changesTitle,
    changes,
    prTitle.label,
    prBody.label,
    diff,
    confirmations,
    error,
    actions,
  );
  section.append(card);
  timeline.append(section);
  state.pendingApproval = {
    event,
    section,
    branch: branch.field,
    commit: commit.field,
    prTitle: prTitle.field,
    prBody: prBody.field,
    changes,
    confirmations,
    error,
  };
  approve.addEventListener("click", () => submitApproval(true));
  reject.addEventListener("click", () => submitApproval(false));
  messageInput.disabled = true;
  sendButton.disabled = true;
  $("#activity-label").textContent = "Waiting for human approval";
  scrollDown();
}

async function submitApproval(approved) {
  const pending = state.pendingApproval;
  if (!pending || state.busy) return;
  const selected = [...pending.changes.querySelectorAll("input:checked")]
    .map((input) => input.dataset.groupId);
  const confirmations = [...pending.confirmations.querySelectorAll("input")];
  if (approved && (!selected.length || confirmations.some((input) => !input.checked))) {
    pending.error.textContent = "Select at least one file group and explicitly confirm all five actions.";
    return;
  }
  const decision = {
    selected_group_ids: selected,
    branch: pending.branch.value.trim(),
    commit_message: pending.commit.value.trim(),
    pr_title: pending.prTitle.value.trim(),
    pr_body: pending.prBody.value.trim(),
  };
  for (const input of confirmations) {
    decision[input.dataset.confirmation] = approved && input.checked;
  }
  state.busy = true;
  pending.error.textContent = "";
  for (const element of pending.section.querySelectorAll("input, textarea, button")) {
    element.disabled = true;
  }
  $("#activity-label").textContent = approved ? "Publishing approved changes" : "Cancelling publish";
  startTimer();
  try {
    const response = await fetch("/api/approval", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        session_id: state.sessionId,
        approval_id: pending.event.approval_id,
        decision,
      }),
    });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "Approval failed");
    }
    await consumeEventStream(response);
    pending.section.classList.add("resolved");
  } catch (caught) {
    renderEvent({type: "error", text: caught.message, elapsed_ms: performance.now() - state.startedAt});
    stopTimer(performance.now() - state.startedAt);
  } finally {
    state.pendingApproval = null;
    state.busy = false;
    messageInput.disabled = false;
    sendButton.disabled = false;
    $("#activity-label").textContent = "Ready for follow-up";
    messageInput.focus();
  }
}

function renderEvent(event) {
  if (event.type === "done") {
    stopTimer(event.elapsed_ms);
    return;
  }
  if (event.type === "tool_result") {
    const card = state.tools.get(event.id);
    if (card) {
      card.querySelector(".tool-state").textContent = `${event.duration_ms}ms · done`;
      const result = document.createElement("pre");
      result.className = "tool-code tool-result";
      result.textContent = event.result;
      card.append(result);
    }
    scrollDown();
    return;
  }

  if (event.type === "approval_required") {
    renderApproval(event);
    return;
  }
  if (event.type === "thinking") {
    const section = eventShell("thinking", "Thinking", event.elapsed_ms);
    const details = document.createElement("details");
    details.className = "thought";
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "Model reasoning";
    const content = document.createElement("pre");
    content.className = "thought-text";
    content.textContent = event.text;
    details.append(summary, content);
    section.append(details);
    timeline.append(section);
  } else if (event.type === "tool_call") {
    const section = eventShell("tool", "Tool call", event.elapsed_ms);
    const details = document.createElement("details");
    details.className = "tool-card";
    details.open = true;
    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.className = "tool-name";
    name.textContent = event.name;
    const toolState = document.createElement("span");
    toolState.className = "tool-state";
    toolState.textContent = "running";
    summary.append(name, toolState);
    const args = document.createElement("pre");
    args.className = "tool-code";
    args.textContent = JSON.stringify(event.arguments, null, 2);
    details.append(summary, args);
    section.append(details);
    timeline.append(section);
    state.tools.set(event.id, details);
  } else if (event.type === "route") {
    const section = eventShell("route", "Developer Agent", event.elapsed_ms);
    textBody(section, event.text);
    timeline.append(section);
  } else if (event.type === "answer") {
    const section = eventShell("answer", event.model || "Agent", event.elapsed_ms);
    textBody(section, event.text);
    timeline.append(section);
  } else if (event.type === "error") {
    const section = eventShell("error", "Error", event.elapsed_ms);
    textBody(section, event.text);
    timeline.append(section);
  } else {
    const section = eventShell("status", "Activity", event.elapsed_ms);
    textBody(section, event.text || "Working…");
    timeline.append(section);
    if (event.model) $("#model-name").textContent = event.model;
  }
  scrollDown();
}

async function createSession(event) {
  event.preventDefault();
  const button = $("#connect");
  const error = $("#setup-error");
  button.disabled = true;
  button.textContent = "Reading repository…";
  error.textContent = "";
  const provider = $("#provider").value;
  const model = $("#model").value.trim();
  const payload = {
    repository: $("#repository").value.trim(),
    provider,
    show_thinking: $("#show-thinking").checked,
  };
  if (provider === "groq") payload.groq_model = model;
  if (provider === "ollama") payload.local_model = model;
  if (provider === "gemini") payload.gemini_model = model;

  try {
    const response = await fetch("/api/session", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start the session");
    state.sessionId = data.session_id;
    state.pendingApproval = null;
    $("#repo-path").textContent = data.repository;
    $("#repo-name").textContent = data.repository.split(/[\\/]/).filter(Boolean).pop();
    $("#model-name").textContent = data.models.join(" → ");
    $("#composer-hint").textContent = "Enter to send · Shift+Enter for newline";
    messageInput.disabled = false;
    sendButton.disabled = false;
    setupLayer.classList.add("hidden");
    messageInput.focus();
  } catch (caught) {
    error.textContent = caught.message;
  } finally {
    button.disabled = false;
    button.textContent = "Connect repository";
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const message = messageInput.value.trim();
  if (!message || !state.sessionId || state.busy) return;
  state.busy = true;
  state.tools.clear();
  messageInput.value = "";
  resizeInput();
  sendButton.disabled = true;
  $("#task-title").textContent = message.length > 56 ? `${message.slice(0, 53)}…` : message;
  $("#activity-label").textContent = "Agent is working";
  addUserMessage(message);
  startTimer();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: state.sessionId, message}),
    });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "Request failed");
    }
    await consumeEventStream(response);
  } catch (caught) {
    renderEvent({type: "error", text: caught.message, elapsed_ms: performance.now() - state.startedAt});
    stopTimer(performance.now() - state.startedAt);
  } finally {
    state.busy = false;
    if (!state.pendingApproval) {
      messageInput.disabled = false;
      sendButton.disabled = false;
      $("#activity-label").textContent = "Ready for follow-up";
      messageInput.focus();
    }
  }
}

function resizeInput() {
  messageInput.style.height = "auto";
  messageInput.style.height = `${Math.min(messageInput.scrollHeight, 160)}px`;
}

setupForm.addEventListener("submit", createSession);
composer.addEventListener("submit", sendMessage);
messageInput.addEventListener("input", resizeInput);
messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
$("#new-session").addEventListener("click", () => {
  if (state.busy || state.pendingApproval) return;
  setupLayer.classList.remove("hidden");
});
$("#provider").addEventListener("change", (event) => {
  const placeholders = {
    auto: "Uses configured provider defaults",
    groq: "llama-3.3-70b-versatile",
    ollama: "qwen3:1.7b",
    gemini: "gemini-3.5-flash-lite",
  };
  $("#model").placeholder = placeholders[event.target.value];
  $("#model").disabled = event.target.value === "auto";
});
