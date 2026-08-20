const $ = (selector) => document.querySelector(selector);

const state = {
  sessionId: null,
  busy: false,
  startedAt: null,
  timerHandle: null,
  tools: new Map(),
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
  } catch (caught) {
    renderEvent({type: "error", text: caught.message, elapsed_ms: performance.now() - state.startedAt});
    stopTimer(performance.now() - state.startedAt);
  } finally {
    state.busy = false;
    sendButton.disabled = false;
    $("#activity-label").textContent = "Ready for follow-up";
    messageInput.focus();
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
  if (state.busy) return;
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
