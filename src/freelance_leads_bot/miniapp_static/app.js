const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const cwdInput = document.querySelector("#cwd");
const output = document.querySelector("#output");
const commandInput = document.querySelector("#command");
const form = document.querySelector("#command-form");
const list = document.querySelector("#list");
const up = document.querySelector("#up");
const statusEl = document.querySelector("#status");
const locked = document.querySelector("#locked");
const cwdShort = document.querySelector("#cwd-short");
const clearOutput = document.querySelector("#clear-output");
const tabs = document.querySelectorAll(".tab");
const panelRail = document.querySelector(".panels");
const bootLinesEl = document.querySelector("#boot-lines");
const bootProgressEl = document.querySelector("#boot-progress");
const bootSpinnerEl = document.querySelector("#boot-spinner");
const bootPercentEl = document.querySelector("#boot-percent");
const panels = {
  terminal: document.querySelector("#terminal-panel"),
  files: document.querySelector("#files-panel"),
  stat: document.querySelector("#stat-panel"),
};
const tabOrder = ["terminal", "files", "stat"];
const tabOffsets = ["4px", "calc(33.3333% + 1.3333px)", "calc(66.6667% - 1.3333px)"];
const panelOffsets = ["0%", "-100%", "-200%"];
const shaderCanvas = document.querySelector("#shader-bg");
const metricEls = {
  cpuValue: document.querySelector("#cpu-value"),
  cpuMeter: document.querySelector("#cpu-meter"),
  loadValue: document.querySelector("#load-value"),
  loadDetail: document.querySelector("#load-detail"),
  loadMeter: document.querySelector("#load-meter"),
  ramValue: document.querySelector("#ram-value"),
  ramMeter: document.querySelector("#ram-meter"),
  swapValue: document.querySelector("#swap-value"),
  swapMeter: document.querySelector("#swap-meter"),
  diskValue: document.querySelector("#disk-value"),
  diskMeter: document.querySelector("#disk-meter"),
  inodeValue: document.querySelector("#inode-value"),
  inodeMeter: document.querySelector("#inode-meter"),
  processValue: document.querySelector("#process-value"),
  uptimeValue: document.querySelector("#uptime-value"),
  networkValue: document.querySelector("#network-value"),
};

tg?.ready();
tg?.expand();

let cwd = "";
let parentPath = "";
let initialDirDone = false;
let initialStatsDone = false;
let bootDone = false;
let bootSpinnerTimer = null;
let bootProgress = 0;
let bootSpinIndex = 0;
let activeTab = "terminal";
const bootSpinnerFrames = ["/", "-", "\\", "|"];

function setBootProgress(value) {
  if (!bootProgressEl) return;
  bootProgress = Math.max(bootProgress, Math.min(100, value));
  bootProgressEl.style.width = `${bootProgress}%`;
  if (bootPercentEl) {
    bootPercentEl.textContent = `${String(Math.round(bootProgress)).padStart(2, "0")}%`;
  }
}

function addBootLine(text, progress) {
  if (bootLinesEl) {
    bootLinesEl.textContent += `${bootSpinnerFrames[bootSpinIndex % bootSpinnerFrames.length]} ${text}\n`;
    bootSpinIndex += 1;
  }
  setBootProgress(progress);
}

function completeBoot() {
  if (bootDone) return;
  bootDone = true;
  addBootLine("console session .......... open", 100);
  if (bootSpinnerTimer) {
    clearInterval(bootSpinnerTimer);
    bootSpinnerTimer = null;
  }
  document.body.classList.add("boot-complete");
}

function tryCompleteBoot() {
  if (initialDirDone && initialStatsDone) {
    completeBoot();
  }
}

function markInitialDirDone() {
  initialDirDone = true;
  tryCompleteBoot();
}

function markInitialStatsDone() {
  initialStatsDone = true;
  tryCompleteBoot();
}

function startBootSequence() {
  setBootProgress(4);
  addBootLine("telegram init data ....... check", 8);
  if (!bootSpinnerEl) return;
  bootSpinnerTimer = setInterval(() => {
    bootSpinnerEl.textContent = bootSpinnerFrames[bootSpinIndex % bootSpinnerFrames.length];
    bootSpinIndex += 1;
  }, 110);
}

startBootSequence();

function startShaderBackground() {
  if (!shaderCanvas) return;
  const gl = shaderCanvas.getContext("webgl", {
    antialias: false,
    alpha: true,
    preserveDrawingBuffer: false,
    powerPreference: "low-power",
  });
  if (!gl) return;

  const vertexSource = `
    attribute vec2 position;
    void main() {
      gl_Position = vec4(position, 0.0, 1.0);
    }
  `;
  const fragmentSource = `
    precision mediump float;
    uniform vec2 iResolution;
    uniform float iTime;

    float hash(vec2 p) {
      return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
    }

    float noise(vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      vec2 u = f * f * (3.0 - 2.0 * f);
      return mix(
        mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
        mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
        u.y
      );
    }

    void main() {
      vec2 uv = gl_FragCoord.xy / iResolution.xy;
      float t = iTime;
      float band = smoothstep(0.88, 1.0, noise(vec2(uv.y * 20.0, t * 0.22)));
      float tear = (noise(vec2(uv.y * 14.0, t * 0.55)) - 0.5) * 0.012 * band;
      uv.x += tear + sin(uv.y * 28.0 + t * 0.9) * 0.002;

      float grain = noise(gl_FragCoord.xy * 0.5 + vec2(t * 12.0, -t * 7.0));
      float scan = sin(uv.y * iResolution.y * 1.7);
      float roll = smoothstep(0.96, 1.0, sin((uv.y + t * 0.035) * 14.0));
      float vignette = smoothstep(0.86, 0.22, distance(uv, vec2(0.5)));

      vec3 base = vec3(0.025, 0.055, 0.065);
      vec3 tint = vec3(0.07, 0.38, 0.32) * (0.18 + grain * 0.34);
      vec3 magenta = vec3(0.22, 0.04, 0.16) * band * 0.28;
      vec3 color = base + tint + magenta;
      color += scan * 0.018;
      color += roll * vec3(0.03, 0.15, 0.12);
      color *= vignette;

      gl_FragColor = vec4(color, 1.0);
    }
  `;

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(gl.getShaderInfoLog(shader) || "Shader compile failed");
    }
    return shader;
  }

  let program;
  try {
    program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSource));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || "Shader link failed");
    }
  } catch (error) {
    console.warn(error);
    return;
  }

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

  const position = gl.getAttribLocation(program, "position");
  const resolution = gl.getUniformLocation(program, "iResolution");
  const time = gl.getUniformLocation(program, "iTime");
  let start = performance.now();

  function resize() {
    const ratio = Math.min(window.devicePixelRatio || 1, 1.25);
    const width = Math.max(1, Math.floor(shaderCanvas.clientWidth * ratio));
    const height = Math.max(1, Math.floor(shaderCanvas.clientHeight * ratio));
    if (shaderCanvas.width !== width || shaderCanvas.height !== height) {
      shaderCanvas.width = width;
      shaderCanvas.height = height;
      gl.viewport(0, 0, width, height);
    }
  }

  function render(now) {
    resize();
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(resolution, shaderCanvas.width, shaderCanvas.height);
    gl.uniform1f(time, (now - start) / 1000);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    requestAnimationFrame(render);
  }

  window.addEventListener("resize", resize);
  requestAnimationFrame(render);
}

startShaderBackground();

function setTelegramInsetVars() {
  const safeTop = Number(tg?.safeAreaInset?.top || 0);
  const contentSafeTop = Number(tg?.contentSafeAreaInset?.top || 0);
  document.documentElement.style.setProperty("--tg-safe-top", `${safeTop}px`);
  document.documentElement.style.setProperty("--tg-content-safe-top", `${contentSafeTop}px`);
}

setTelegramInsetVars();
tg?.onEvent?.("safeAreaChanged", setTelegramInsetVars);
tg?.onEvent?.("contentSafeAreaChanged", setTelegramInsetVars);

function setStatus(text) {
  statusEl.textContent = text;
}

function showTab(name) {
  if (!tabOrder.includes(name)) return;
  activeTab = name;
  const index = tabOrder.indexOf(name);
  document.documentElement.style.setProperty("--active-tab-index", index);
  document.documentElement.style.setProperty("--active-tab-left", tabOffsets[index]);
  document.documentElement.style.setProperty("--active-panel-index", index);
  document.documentElement.style.setProperty("--active-panel-offset", panelOffsets[index]);
  for (const tab of tabs) {
    tab.classList.toggle("active", tab.dataset.tab === name);
    tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
  }
  for (const [panelName, panel] of Object.entries(panels)) {
    panel.classList.toggle("active", panelName === name);
  }
  if (name === "terminal") {
    commandInput.focus();
  }
}

function append(text) {
  output.textContent += text;
  output.scrollTop = output.scrollHeight;
}

function appendCommand(command) {
  append(`${output.textContent ? "\n" : ""}$ ${command}\n`);
}

function setLocked(text) {
  locked.querySelector("span").textContent = text;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `tma ${initData}`,
    },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function downloadEntry(entry) {
  setStatus("download");
  try {
    const data = await api("/miniapp/api/download-link", { path: entry.path });
    const filename = data.filename || entry.name;
    let handled = false;
    if (tg?.downloadFile) {
      try {
        tg.downloadFile({ url: data.url, file_name: filename }, (accepted) => {
          setStatus("ready");
          if (accepted === false) append("\n[download] cancelled\n");
        });
        handled = true;
      } catch (error) {
        handled = false;
      }
    }
    if (handled) return;
    const link = document.createElement("a");
    link.href = data.url;
    link.download = filename;
    link.target = "_blank";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setStatus("ready");
  } catch (error) {
    setStatus("error");
    append(`\n[download] ${error.message}\n`);
  }
}

function formatSize(size) {
  if (size === null || size === undefined) return "";
  if (size < 1024) return `${size}b`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)}k`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)}m`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)}g`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "--";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function setMeter(el, percent) {
  el.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

async function loadStats(isInitial = false) {
  if (isInitial) addBootLine("system metrics ........... request", 32);
  try {
    const data = await api("/miniapp/api/stats");
    metricEls.cpuValue.textContent = `${data.cpu_percent}%`;
    metricEls.loadValue.textContent = data.load?.join(" / ") || "--";
    metricEls.loadDetail.textContent = `${data.cpu_count} cores`;
    metricEls.ramValue.textContent = `${formatSize(data.memory_used)} / ${formatSize(data.memory_total)}`;
    metricEls.swapValue.textContent = data.swap_total ? `${formatSize(data.swap_used)} / ${formatSize(data.swap_total)}` : "off";
    metricEls.diskValue.textContent = `${formatSize(data.disk_used)} / ${formatSize(data.disk_total)}`;
    metricEls.inodeValue.textContent = `${data.inode_percent}%`;
    metricEls.processValue.textContent = data.process_count;
    metricEls.uptimeValue.textContent = `uptime ${formatDuration(data.uptime_seconds)}`;
    metricEls.networkValue.textContent = `${formatSize(data.rx_bytes)} / ${formatSize(data.tx_bytes)}`;
    setMeter(metricEls.cpuMeter, data.cpu_percent);
    setMeter(metricEls.loadMeter, data.load_percent);
    setMeter(metricEls.ramMeter, data.memory_percent);
    setMeter(metricEls.swapMeter, data.swap_percent);
    setMeter(metricEls.diskMeter, data.disk_percent);
    setMeter(metricEls.inodeMeter, data.inode_percent);
    if (isInitial) addBootLine("system metrics ........... ok", 58);
  } catch (error) {
    for (const [key, el] of Object.entries(metricEls)) {
      if (key.endsWith("Value")) el.textContent = "err";
    }
    if (isInitial) addBootLine("system metrics ........... error", 58);
  } finally {
    if (isInitial) markInitialStatsDone();
  }
}

async function loadDir(path, isInitial = false) {
  setStatus("loading");
  if (isInitial) addBootLine("workspace listing ........ request", 24);
  try {
    const data = await api("/miniapp/api/list", { path });
    cwd = data.cwd;
    parentPath = data.parent;
    cwdInput.value = cwd;
    cwdShort.textContent = cwd;
    document.body.classList.add("authorized");
    list.innerHTML = "";
    for (const entry of data.entries) {
      const row = document.createElement("div");
      row.className = "entry";
      row.innerHTML = `
        <button class="entry-main" type="button">
          <span>${entry.is_dir ? "▸" : "·"}</span>
          <span class="name"></span>
          <span class="meta">${entry.is_dir ? "dir" : formatSize(entry.size)}</span>
        </button>
        <button class="entry-download" type="button" title="Download" aria-label="Download ${entry.name}">⇩</button>
      `;
      row.querySelector(".name").textContent = entry.name;
      row.querySelector(".entry-main").addEventListener("click", () => {
        if (entry.is_dir) {
          loadDir(entry.path);
        } else {
          commandInput.value = `sed -n '1,220p' ${JSON.stringify(entry.path)}`;
          showTab("terminal");
          commandInput.focus();
        }
      });
      row.querySelector(".entry-download").addEventListener("click", () => downloadEntry(entry));
      list.appendChild(row);
    }
    setStatus("ready");
    if (isInitial) addBootLine("workspace listing ........ ok", 84);
  } catch (error) {
    setStatus("error");
    setLocked(error.message);
    append(`\n[list] ${error.message}\n`);
    if (isInitial) addBootLine("workspace listing ........ error", 84);
  } finally {
    if (isInitial) markInitialDirDone();
  }
}

async function runCommand(command) {
  appendCommand(command);
  setStatus("running");
  try {
    const data = await api("/miniapp/api/exec", { command, cwd });
    cwd = data.cwd;
    cwdInput.value = cwd;
    append(data.output || `(exit ${data.returncode}, no output)\n`);
    if (data.truncated) append("\n[output truncated]\n");
    append(`\n[exit ${data.returncode}, ${data.duration_ms}ms]\n`);
    await loadDir(cwd);
  } catch (error) {
    append(`[error] ${error.message}\n`);
    setStatus("error");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const command = commandInput.value.trim();
  if (!command) return;
  commandInput.value = "";
  await runCommand(command);
});

cwdInput.addEventListener("change", () => loadDir(cwdInput.value));
up.addEventListener("click", () => {
  if (parentPath) loadDir(parentPath);
});

clearOutput.addEventListener("click", () => {
  output.textContent = "";
  showTab("terminal");
});

for (const tab of tabs) {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
}

function setupPanelSwipe() {
  if (!panelRail) return;
  let startX = 0;
  let startY = 0;
  let deltaX = 0;
  let deltaY = 0;
  let swiping = false;
  let suppressNextClick = false;

  function shouldIgnoreSwipeTarget(target) {
    if (target.closest("input, textarea, select, a")) return true;
    if (target.closest(".entry-main, .entry-download")) return false;
    return Boolean(target.closest("button"));
  }

  panelRail.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) return;
    const target = event.target;
    if (shouldIgnoreSwipeTarget(target)) return;
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
    deltaX = 0;
    deltaY = 0;
    swiping = true;
  }, { passive: true });

  panelRail.addEventListener("touchmove", (event) => {
    if (!swiping || event.touches.length !== 1) return;
    deltaX = event.touches[0].clientX - startX;
    deltaY = event.touches[0].clientY - startY;
    if (Math.abs(deltaX) > 12 && Math.abs(deltaX) > Math.abs(deltaY) * 1.25) {
      event.preventDefault();
    }
  }, { passive: false });

  panelRail.addEventListener("touchend", () => {
    if (!swiping) return;
    swiping = false;
    const currentIndex = tabOrder.indexOf(activeTab);
    const shouldSwitch = Math.abs(deltaX) >= 58 && Math.abs(deltaX) > Math.abs(deltaY) * 1.35;
    if (Math.abs(deltaX) > 24 && Math.abs(deltaX) > Math.abs(deltaY) * 1.2) {
      suppressNextClick = true;
    }
    if (!shouldSwitch) return;
    const nextIndex = currentIndex + (deltaX < 0 ? 1 : -1);
    if (nextIndex >= 0 && nextIndex < tabOrder.length) {
      showTab(tabOrder[nextIndex]);
    }
  }, { passive: true });

  panelRail.addEventListener("click", (event) => {
    if (!suppressNextClick) return;
    suppressNextClick = false;
    if (!event.target.closest(".entry-main, .entry-download")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
}

setupPanelSwipe();

if (!initData) {
  setStatus("open in Telegram");
  setLocked("Открой Mini App из кнопки Telegram, иначе нет initData для авторизации.");
  addBootLine("telegram init data ....... missing", 100);
  markInitialDirDone();
  markInitialStatsDone();
} else {
  addBootLine("telegram init data ....... ok", 16);
  loadDir(null, true);
  loadStats(true);
  setInterval(loadStats, 5000);
}
