const fs = require('fs');
const http = require('http');
const https = require('https');
const path = require('path');
const childProcess = require('child_process');
const vscode = require('vscode');

let extensionContext;
let assistantProvider;
let identityProvider;
let sessionsProvider;
let statusBar;

function activate(context) {
  extensionContext = context;
  const api = new ControlPlaneApi();
  assistantProvider = new AssistantProvider(context.extensionUri);
  identityProvider = new IdentityProvider(api);
  sessionsProvider = new SessionsProvider(api);

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 80);
  statusBar.command = 'nyansa.refresh';
  statusBar.text = '$(shield) Nyansa';
  statusBar.tooltip = 'Nyansa control-plane status';
  statusBar.show();

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('nyansa.assistant', assistantProvider),
    vscode.window.registerTreeDataProvider('nyansa.identity', identityProvider),
    vscode.window.registerTreeDataProvider('nyansa.sessions', sessionsProvider),
    statusBar,
    vscode.commands.registerCommand('nyansa.refresh', refreshAll),
    vscode.commands.registerCommand('nyansa.openAssistant', openAssistant),
    vscode.commands.registerCommand('nyansa.configureEnvironment', configureEnvironment),
    vscode.commands.registerCommand('nyansa.startInteractive', startInteractive),
    vscode.commands.registerCommand('nyansa.runPrompt', runPrompt),
    vscode.commands.registerCommand('nyansa.openControlPlane', openControlPlane),
    vscode.commands.registerCommand('nyansa.showCapabilities', showCapabilities),
    vscode.commands.registerCommand('nyansa.revokeSession', revokeSession)
  );

  refreshAll();
}

function deactivate() {}

async function refreshAll() {
  const api = new ControlPlaneApi();
  identityProvider.api = api;
  sessionsProvider.api = api;
  await Promise.allSettled([identityProvider.refresh(), sessionsProvider.refresh()]);
  if (assistantProvider) {
    assistantProvider.postState();
  }
  const actor = identityProvider.actor;
  if (actor) {
    statusBar.text = `$(shield) ${actor.role} · ${actor.user_id}`;
    statusBar.tooltip = `Nyansa: ${actor.user_id} in ${actor.organization_id || 'no organization'}`;
  } else {
    statusBar.text = '$(shield) Nyansa';
    statusBar.tooltip = identityProvider.lastError || 'Nyansa control plane';
  }
}

async function openAssistant() {
  await vscode.commands.executeCommand('workbench.view.extension.nyansa');
  await vscode.commands.executeCommand('nyansa.assistant.focus');
}

async function startInteractive() {
  const terminal = createAgentTerminal();
  terminal.show();
  terminal.sendText(getConfig().cliCommand);
}

async function runPrompt() {
  const prompt = await vscode.window.showInputBox({
    title: 'Nyansa Prompt',
    prompt: 'Run the coding agent in this workspace',
    ignoreFocusOut: true
  });
  if (!prompt) {
    return;
  }
  const terminal = createAgentTerminal();
  terminal.show();
  terminal.sendText(`${getConfig().cliCommand} ${shellQuote(prompt)}`);
}

async function configureEnvironment() {
  const config = getConfig();
  const choice = await vscode.window.showQuickPick(
    [
      { label: 'Python interpreter', description: config.pythonPath || 'Not set' },
      { label: 'Env file', description: config.envFile },
      { label: 'CLI command', description: config.cliCommand },
      { label: 'Control plane URL', description: config.controlPlaneUrl },
      { label: 'Extra environment variables', description: `${Object.keys(config.extraEnv).length} set` }
    ],
    {
      title: 'Configure Nyansa Environment',
      placeHolder: 'Choose what to configure'
    }
  );
  if (!choice) {
    return;
  }
  if (choice.label === 'Python interpreter') {
    await choosePythonInterpreter();
  } else if (choice.label === 'Env file') {
    await promptSetting('envFile', 'Workspace-relative env file', config.envFile);
  } else if (choice.label === 'CLI command') {
    await promptSetting('cliCommand', 'Command used to launch Nyansa', config.cliCommand);
  } else if (choice.label === 'Control plane URL') {
    await promptSetting('controlPlaneUrl', 'Control-plane base URL', config.controlPlaneUrl);
  } else if (choice.label === 'Extra environment variables') {
    await editExtraEnv();
  }
  await refreshAll();
  assistantProvider?.postState();
}

async function promptSetting(key, title, currentValue) {
  const value = await vscode.window.showInputBox({
    title,
    value: currentValue || '',
    ignoreFocusOut: true
  });
  if (value === undefined) {
    return;
  }
  await vscode.workspace.getConfiguration('nyansa').update(key, value, vscode.ConfigurationTarget.Workspace);
}

async function editExtraEnv() {
  const config = getConfig();
  const current = Object.entries(config.extraEnv)
    .map(([key, value]) => `${key}=${value}`)
    .join('\n');
  const value = await vscode.window.showInputBox({
    title: 'Extra environment variables',
    prompt: 'Enter KEY=value pairs separated by semicolons. Existing values are shown newline-separated.',
    value: current,
    ignoreFocusOut: true
  });
  if (value === undefined) {
    return;
  }
  await vscode.workspace
    .getConfiguration('nyansa')
    .update('extraEnv', parseEnvAssignments(value), vscode.ConfigurationTarget.Workspace);
}

async function choosePythonInterpreter() {
  const config = getConfig();
  const candidates = pythonCandidates();
  const picked = await vscode.window.showQuickPick(
    [
      ...candidates.map((candidate) => ({
        label: path.basename(candidate),
        description: candidate
      })),
      { label: 'Browse...', description: 'Pick an interpreter from disk' },
      { label: 'Clear setting', description: config.pythonPath || 'No interpreter configured' }
    ],
    {
      title: 'Nyansa Python Interpreter',
      placeHolder: 'Select the Python used by Nyansa commands'
    }
  );
  if (!picked) {
    return;
  }
  if (picked.label === 'Browse...') {
    const files = await vscode.window.showOpenDialog({
      title: 'Select Python Interpreter',
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false
    });
    if (!files || files.length === 0) {
      return;
    }
    await vscode.workspace
      .getConfiguration('nyansa')
      .update('pythonPath', files[0].fsPath, vscode.ConfigurationTarget.Workspace);
    return;
  }
  await vscode.workspace
    .getConfiguration('nyansa')
    .update('pythonPath', picked.label === 'Clear setting' ? '' : picked.description, vscode.ConfigurationTarget.Workspace);
}

async function openControlPlane() {
  await vscode.env.openExternal(vscode.Uri.parse(getConfig().controlPlaneUrl));
}

async function showCapabilities() {
  const terminal = createAgentTerminal();
  terminal.show();
  terminal.sendText(`${getConfig().cliCommand} "/capabilities"`);
}

async function revokeSession(item) {
  if (!item || !item.session || item.session.status !== 'active') {
    vscode.window.showWarningMessage('Select an active session to revoke.');
    return;
  }
  const answer = await vscode.window.showWarningMessage(
    `Revoke session ${item.session.id} for ${item.session.user_id}?`,
    { modal: true },
    'Revoke'
  );
  if (answer !== 'Revoke') {
    return;
  }
  try {
    await new ControlPlaneApi().post(`/sessions/${encodeURIComponent(item.session.id)}/revoke`, {});
    vscode.window.showInformationMessage('Nyansa session revoked.');
    await refreshAll();
  } catch (error) {
    vscode.window.showErrorMessage(`Could not revoke session: ${error.message}`);
  }
}

function createAgentTerminal() {
  const workspace = getWorkspaceFolder();
  const env = buildAgentEnv();
  return vscode.window.createTerminal({
    name: 'Nyansa',
    cwd: workspace,
    env
  });
}

function getWorkspaceFolder() {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    return undefined;
  }
  return folders[0].uri.fsPath;
}

function getConfig() {
  const cfg = vscode.workspace.getConfiguration('nyansa');
  return {
    controlPlaneUrl: cfg.get('controlPlaneUrl', 'http://127.0.0.1:8080').replace(/\/+$/, ''),
    tokenEnv: cfg.get('tokenEnv', 'NYANSA_CONTROL_TOKEN'),
    envFile: cfg.get('envFile', '.env'),
    cliCommand: cfg.get('cliCommand', 'uv run nyansa'),
    pythonPath: cfg.get('pythonPath', ''),
    extraEnv: cfg.get('extraEnv', {})
  };
}

function buildAgentEnv() {
  const config = getConfig();
  const env = {
    ...loadWorkspaceEnv(),
    ...stringMap(config.extraEnv)
  };

  if (config.pythonPath) {
    const pythonPath = resolveWorkspacePath(config.pythonPath);
    env.PYTHON = pythonPath;
    const binDir = path.dirname(pythonPath);
    env.PATH = [binDir, process.env.PATH].filter(Boolean).join(path.delimiter);

    const venvRoot = inferVirtualEnvRoot(pythonPath);
    if (venvRoot) {
      env.VIRTUAL_ENV = venvRoot;
    }
  }

  return env;
}

function loadWorkspaceEnv() {
  const workspace = getWorkspaceFolder();
  const env = {};
  const config = getConfig();
  if (!workspace) {
    return env;
  }
  for (const envFile of envFileCandidates(config.envFile)) {
    const envPath = path.join(workspace, envFile);
    if (!fs.existsSync(envPath)) {
      continue;
    }
    const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)\s*$/);
      if (!match) {
        continue;
      }
      env[match[1]] = stripQuotes(match[2]);
    }
  }
  return env;
}

function stringMap(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {};
  }
  const result = {};
  for (const [key, raw] of Object.entries(value)) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && raw !== undefined && raw !== null) {
      result[key] = String(raw);
    }
  }
  return result;
}

function parseEnvAssignments(text) {
  const env = {};
  for (const part of String(text).split(/[;\n]/)) {
    const trimmed = part.trim();
    if (!trimmed) {
      continue;
    }
    const match = trimmed.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (match) {
      env[match[1]] = stripQuotes(match[2]);
    }
  }
  return env;
}

function pythonCandidates() {
  const workspace = getWorkspaceFolder();
  const candidates = new Set();
  const configured = vscode.workspace.getConfiguration('python').get('defaultInterpreterPath', '');
  if (configured) {
    candidates.add(resolveWorkspacePath(configured));
  }
  if (workspace) {
    for (const relative of [
      '.venv/bin/python',
      '.venv/Scripts/python.exe',
      'venv/bin/python',
      'venv/Scripts/python.exe'
    ]) {
      const candidate = path.join(workspace, relative);
      if (fs.existsSync(candidate)) {
        candidates.add(candidate);
      }
    }
  }
  return Array.from(candidates);
}

function resolveWorkspacePath(value) {
  if (!value) {
    return value;
  }
  if (path.isAbsolute(value)) {
    return value;
  }
  const workspace = getWorkspaceFolder();
  return workspace ? path.join(workspace, value) : value;
}

function inferVirtualEnvRoot(pythonPath) {
  const normalized = path.normalize(pythonPath);
  const parent = path.basename(path.dirname(normalized)).toLowerCase();
  if (parent !== 'bin' && parent !== 'scripts') {
    return '';
  }
  const root = path.dirname(path.dirname(normalized));
  const rootName = path.basename(root).toLowerCase();
  return rootName.includes('venv') ? root : '';
}

function envSummary() {
  const config = getConfig();
  const env = buildAgentEnv();
  const envFileLoaded = envFileCandidates(config.envFile).filter((candidate) => {
    const workspace = getWorkspaceFolder();
    return workspace && fs.existsSync(path.join(workspace, candidate));
  });
  return {
    workspace: getWorkspaceFolder() || 'No workspace folder',
    cliCommand: config.cliCommand,
    controlPlaneUrl: config.controlPlaneUrl,
    tokenEnv: config.tokenEnv,
    tokenPresent: Boolean({ ...process.env, ...env }[config.tokenEnv]),
    envFile: config.envFile,
    envFileLoaded,
    pythonPath: config.pythonPath ? resolveWorkspacePath(config.pythonPath) : '',
    extraEnvCount: Object.keys(stringMap(config.extraEnv)).length
  };
}

function envFileCandidates(configured) {
  return Array.from(new Set([configured, '.env', '.dev/env'].filter(Boolean)));
}

function stripQuotes(value) {
  const trimmed = value.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

class ControlPlaneApi {
  constructor() {
    this.config = getConfig();
    this.env = { ...process.env, ...buildAgentEnv() };
  }

  async get(route) {
    return this.request('GET', route);
  }

  async post(route, body) {
    return this.request('POST', route, body);
  }

  async request(method, route, body) {
    const token = this.env[this.config.tokenEnv];
    if (!token) {
      throw new Error(`Missing ${this.config.tokenEnv}. Issue a CLI token from the web UI, put it in ${this.config.envFile}, or set nyansa.envFile.`);
    }
    const url = new URL(`${this.config.controlPlaneUrl}${route}`);
    const payload = body ? Buffer.from(JSON.stringify(body)) : undefined;
    const headers = {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json'
    };
    if (payload) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = String(payload.length);
    }

    return new Promise((resolve, reject) => {
      const transport = url.protocol === 'https:' ? https : http;
      const req = transport.request(
        url,
        {
          method,
          headers,
          timeout: 7000
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let parsed = {};
            if (text) {
              try {
                parsed = JSON.parse(text);
              } catch {
                parsed = { detail: text };
              }
            }
            if (res.statusCode && res.statusCode >= 400) {
              reject(new Error(parsed.detail || `HTTP ${res.statusCode}`));
              return;
            }
            resolve(parsed);
          });
        }
      );
      req.on('error', reject);
      req.on('timeout', () => req.destroy(new Error('Control-plane request timed out')));
      if (payload) {
        req.write(payload);
      }
      req.end();
    });
  }
}

class AssistantProvider {
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
    this.view = null;
    this.messages = [];
    this.running = false;
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri]
    };
    webviewView.webview.html = this.html(webviewView.webview);
    webviewView.webview.onDidReceiveMessage((message) => this.handleMessage(message));
    this.postState();
  }

  async handleMessage(message) {
    if (!message || typeof message.type !== 'string') {
      return;
    }
    if (message.type === 'ready') {
      this.postState();
    } else if (message.type === 'runPrompt') {
      await this.runPrompt(message.prompt || '');
    } else if (message.type === 'startTerminal') {
      await startInteractive();
    } else if (message.type === 'runCliCommand') {
      await this.runPrompt(message.command || '');
    } else if (message.type === 'configureEnvironment') {
      await configureEnvironment();
    } else if (message.type === 'choosePython') {
      await choosePythonInterpreter();
      this.postState();
    } else if (message.type === 'openControlPlane') {
      await openControlPlane();
    } else if (message.type === 'openEnvFile') {
      await openEnvFile();
    } else if (message.type === 'saveSettings') {
      await this.saveSettings(message.settings || {});
    }
  }

  async saveSettings(settings) {
    const cfg = vscode.workspace.getConfiguration('nyansa');
    const target = vscode.ConfigurationTarget.Workspace;
    const updates = [
      ['cliCommand', settings.cliCommand],
      ['controlPlaneUrl', settings.controlPlaneUrl],
      ['tokenEnv', settings.tokenEnv],
      ['envFile', settings.envFile],
      ['pythonPath', settings.pythonPath],
      ['extraEnv', parseEnvAssignments(settings.extraEnv || '')]
    ];
    for (const [key, value] of updates) {
      if (value !== undefined) {
        await cfg.update(key, value, target);
      }
    }
    vscode.window.showInformationMessage('Nyansa environment updated.');
    await refreshAll();
    this.postState();
  }

  async runPrompt(prompt) {
    const trimmed = String(prompt).trim();
    if (!trimmed || this.running) {
      return;
    }
    this.running = true;
    this.messages.push({ role: 'user', text: trimmed });
    this.postState();

    try {
      const output = await runNyansaOnce(trimmed);
      this.messages.push({ role: 'assistant', text: output || '(no output)' });
    } catch (error) {
      this.messages.push({ role: 'error', text: error.message });
    } finally {
      this.running = false;
      this.postState();
      await refreshAll();
    }
  }

  postState() {
    if (!this.view) {
      return;
    }
    const config = getConfig();
    this.view.webview.postMessage({
      type: 'state',
      running: this.running,
      messages: this.messages,
      actor: identityProvider?.actor || null,
      lastError: identityProvider?.lastError || '',
      env: envSummary(),
      settings: {
        cliCommand: config.cliCommand,
        controlPlaneUrl: config.controlPlaneUrl,
        tokenEnv: config.tokenEnv,
        envFile: config.envFile,
        pythonPath: config.pythonPath,
        extraEnv: Object.entries(stringMap(config.extraEnv))
          .map(([key, value]) => `${key}=${value}`)
          .join('\n')
      }
    });
  }

  html(webview) {
    const nonce = getNonce();
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src 'nonce-${nonce}'`
    ].join('; ');
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nyansa Assistant</title>
  <style>
    :root {
      color-scheme: light dark;
      --border: var(--vscode-panel-border);
      --muted: var(--vscode-descriptionForeground);
      --bg-soft: var(--vscode-sideBar-background);
      --accent: var(--vscode-button-background);
      --accent-fg: var(--vscode-button-foreground);
      --danger: var(--vscode-errorForeground);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 0;
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
    }
    .app { min-height: 100vh; display: flex; flex-direction: column; }
    header {
      padding: 14px 14px 10px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-soft);
    }
    h1 { margin: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: 0; }
    .status { display: grid; gap: 6px; color: var(--muted); }
    .status-row { display: flex; gap: 8px; align-items: center; min-width: 0; }
    .status-row span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--danger); flex: 0 0 auto; }
    .dot.connected { background: #2ea043; }
    main { flex: 1; min-height: 0; display: grid; grid-template-rows: 1fr auto; }
    .messages { overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
    .empty {
      margin: auto;
      max-width: 440px;
      color: var(--muted);
      line-height: 1.45;
      text-align: center;
    }
    .msg {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      background: var(--vscode-editorWidget-background);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .msg.user { border-color: var(--vscode-focusBorder); }
    .msg.error { border-color: var(--danger); color: var(--danger); }
    .label { color: var(--muted); font-size: 11px; text-transform: uppercase; margin-bottom: 6px; }
    .composer { border-top: 1px solid var(--border); padding: 10px; background: var(--bg-soft); }
    textarea, input {
      width: 100%;
      color: var(--vscode-input-foreground);
      background: var(--vscode-input-background);
      border: 1px solid var(--vscode-input-border, var(--border));
      border-radius: 4px;
      padding: 8px;
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
    }
    textarea { min-height: 92px; resize: vertical; }
    button {
      border: 0;
      border-radius: 4px;
      padding: 7px 10px;
      color: var(--accent-fg);
      background: var(--accent);
      cursor: pointer;
      font: inherit;
    }
    button.secondary {
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
    }
    button.icon { min-width: 32px; padding: 7px 8px; }
    button:disabled { opacity: .55; cursor: default; }
    .toolbar, .quick-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .toolbar { margin-top: 8px; justify-content: space-between; }
    .quick-actions { margin-top: 8px; }
    details {
      margin-top: 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--vscode-editorWidget-background);
    }
    summary { cursor: pointer; padding: 8px 10px; }
    .settings { padding: 0 10px 10px; display: grid; gap: 8px; }
    .pair { display: grid; gap: 4px; }
    .pair label { color: var(--muted); font-size: 12px; }
    .env-grid { display: grid; gap: 4px; color: var(--muted); }
    code { font-family: var(--vscode-editor-font-family); }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <h1>Nyansa Assistant</h1>
      <div class="status">
        <div class="status-row"><span id="dot" class="dot"></span><span id="identity">Checking identity...</span></div>
        <div class="status-row"><span>CLI</span><span id="cli"></span></div>
        <div class="status-row"><span>Python</span><span id="python"></span></div>
      </div>
    </header>
    <main>
      <section id="messages" class="messages"></section>
      <section class="composer">
        <textarea id="prompt" placeholder="Ask Nyansa to inspect, edit, test, or explain this workspace"></textarea>
        <div class="quick-actions">
          <button class="secondary" data-command="/help">/help</button>
          <button class="secondary" data-command="/capabilities">/capabilities</button>
          <button class="secondary" data-command="/checkpoints">/checkpoints</button>
          <button class="secondary" data-command="/sessions">/sessions</button>
        </div>
        <div class="toolbar">
          <div class="toolbar">
            <button id="run">Run</button>
            <button id="terminal" class="secondary">Terminal</button>
          </div>
          <div class="toolbar">
            <button id="env" class="secondary icon" title="Configure environment">$(gear)</button>
            <button id="control" class="secondary icon" title="Open control plane">$(globe)</button>
          </div>
        </div>
        <details>
          <summary>Environment</summary>
          <div class="settings">
            <div class="env-grid" id="envSummary"></div>
            <div class="pair"><label for="cliCommand">CLI command</label><input id="cliCommand"></div>
            <div class="pair"><label for="controlPlaneUrl">Control plane URL</label><input id="controlPlaneUrl"></div>
            <div class="pair"><label for="tokenEnv">Token env var</label><input id="tokenEnv"></div>
            <div class="pair"><label for="envFile">Env file</label><input id="envFile"></div>
            <div class="pair"><label for="pythonPath">Python path</label><input id="pythonPath"></div>
            <div class="toolbar">
              <button id="choosePython" class="secondary">Choose Python</button>
              <button id="openEnv" class="secondary">Open Env File</button>
            </div>
            <div class="pair"><label for="extraEnv">Extra env</label><textarea id="extraEnv" placeholder="KEY=value"></textarea></div>
            <button id="saveSettings">Save Environment</button>
          </div>
        </details>
      </section>
    </main>
  </div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const els = {
      dot: document.getElementById('dot'),
      identity: document.getElementById('identity'),
      cli: document.getElementById('cli'),
      python: document.getElementById('python'),
      messages: document.getElementById('messages'),
      prompt: document.getElementById('prompt'),
      run: document.getElementById('run'),
      terminal: document.getElementById('terminal'),
      env: document.getElementById('env'),
      control: document.getElementById('control'),
      envSummary: document.getElementById('envSummary'),
      cliCommand: document.getElementById('cliCommand'),
      controlPlaneUrl: document.getElementById('controlPlaneUrl'),
      tokenEnv: document.getElementById('tokenEnv'),
      envFile: document.getElementById('envFile'),
      pythonPath: document.getElementById('pythonPath'),
      choosePython: document.getElementById('choosePython'),
      openEnv: document.getElementById('openEnv'),
      extraEnv: document.getElementById('extraEnv'),
      saveSettings: document.getElementById('saveSettings')
    };
    let state = { messages: [], running: false };
    window.addEventListener('message', event => {
      if (event.data.type === 'state') {
        state = event.data;
        render();
      }
    });
    function render() {
      els.run.disabled = state.running;
      els.dot.className = 'dot' + (state.actor ? ' connected' : '');
      els.identity.textContent = state.actor
        ? state.actor.user_id + ' · ' + state.actor.role
        : (state.lastError || 'Not connected');
      els.cli.textContent = state.env.cliCommand;
      els.python.textContent = state.env.pythonPath || 'Default shell Python';
      els.envSummary.innerHTML = [
        ['Workspace', state.env.workspace],
        ['Token', state.env.tokenPresent ? state.env.tokenEnv + ' present' : state.env.tokenEnv + ' missing'],
        ['Env file', state.env.envFileLoaded.length ? state.env.envFileLoaded.join(', ') : state.env.envFile + ' not found'],
        ['Extra env', state.env.extraEnvCount + ' variables']
      ].map(([k, v]) => '<div><strong>' + escapeHtml(k) + ':</strong> ' + escapeHtml(v) + '</div>').join('');
      if (state.settings) {
        els.cliCommand.value = state.settings.cliCommand || '';
        els.controlPlaneUrl.value = state.settings.controlPlaneUrl || '';
        els.tokenEnv.value = state.settings.tokenEnv || '';
        els.envFile.value = state.settings.envFile || '';
        els.pythonPath.value = state.settings.pythonPath || '';
        els.extraEnv.value = state.settings.extraEnv || '';
      }
      if (!state.messages.length) {
        els.messages.innerHTML = '<div class="empty">Ask Nyansa from VS Code, open a governed terminal, or run quick commands like <code>/capabilities</code>. Environment settings below apply to both the panel and terminal.</div>';
      } else {
        els.messages.innerHTML = state.messages.map(message =>
          '<article class="msg ' + escapeHtml(message.role) + '"><div class="label">' +
          escapeHtml(message.role) + '</div>' + escapeHtml(message.text) + '</article>'
        ).join('');
        els.messages.scrollTop = els.messages.scrollHeight;
      }
    }
    function runPrompt(value) {
      const prompt = (value || els.prompt.value).trim();
      if (!prompt) return;
      vscode.postMessage({ type: 'runPrompt', prompt });
      els.prompt.value = '';
    }
    function saveSettings() {
      vscode.postMessage({
        type: 'saveSettings',
        settings: {
          cliCommand: els.cliCommand.value,
          controlPlaneUrl: els.controlPlaneUrl.value,
          tokenEnv: els.tokenEnv.value,
          envFile: els.envFile.value,
          pythonPath: els.pythonPath.value,
          extraEnv: els.extraEnv.value
        }
      });
    }
    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }
    els.run.addEventListener('click', () => runPrompt());
    els.prompt.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        runPrompt();
      }
    });
    els.terminal.addEventListener('click', () => vscode.postMessage({ type: 'startTerminal' }));
    els.env.addEventListener('click', () => vscode.postMessage({ type: 'configureEnvironment' }));
    els.control.addEventListener('click', () => vscode.postMessage({ type: 'openControlPlane' }));
    els.choosePython.addEventListener('click', () => vscode.postMessage({ type: 'choosePython' }));
    els.openEnv.addEventListener('click', () => vscode.postMessage({ type: 'openEnvFile' }));
    els.saveSettings.addEventListener('click', saveSettings);
    document.querySelectorAll('[data-command]').forEach(button => {
      button.addEventListener('click', () => runPrompt(button.dataset.command));
    });
    vscode.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}

async function runNyansaOnce(prompt) {
  const config = getConfig();
  const workspace = getWorkspaceFolder();
  const command = `${config.cliCommand} ${shellQuote(prompt)}`;
  return new Promise((resolve, reject) => {
    childProcess.exec(
      command,
      {
        cwd: workspace,
        env: { ...process.env, ...buildAgentEnv() },
        maxBuffer: 1024 * 1024 * 8,
        timeout: 1000 * 60 * 10
      },
      (error, stdout, stderr) => {
        const output = [stdout, stderr ? `\n${stderr}` : ''].join('').trim();
        if (error) {
          reject(new Error(output || error.message));
          return;
        }
        resolve(output);
      }
    );
  });
}

async function openEnvFile() {
  const workspace = getWorkspaceFolder();
  if (!workspace) {
    vscode.window.showWarningMessage('Open a workspace before editing the Nyansa env file.');
    return;
  }
  const envPath = path.join(workspace, getConfig().envFile);
  if (!fs.existsSync(envPath)) {
    fs.mkdirSync(path.dirname(envPath), { recursive: true });
    fs.writeFileSync(envPath, '', { encoding: 'utf8', flag: 'wx' });
  }
  await vscode.window.showTextDocument(vscode.Uri.file(envPath));
}

class IdentityProvider {
  constructor(api) {
    this.api = api;
    this.actor = null;
    this.lastError = '';
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  async refresh() {
    try {
      this.actor = await this.api.get('/me');
      this.lastError = '';
    } catch (error) {
      this.actor = null;
      this.lastError = error.message;
    }
    this.emitter.fire();
  }

  getTreeItem(item) {
    return item;
  }

  getChildren() {
    if (!this.actor) {
      return [new InfoItem('Not connected', this.lastError || 'Set token env and start the control plane', 'warning')];
    }
    const policy = this.actor.usage_policy || {};
    return [
      new InfoItem('User', this.actor.user_id, 'account'),
      new InfoItem('Role', this.actor.role, 'shield'),
      new InfoItem('Organization', this.actor.organization_id || 'none', 'organization'),
      new InfoItem('Allowed tools', this.actor.allowed_tools ? String(this.actor.allowed_tools.length) : 'role default', 'tools'),
      new InfoItem('Daily cap', policy.requests_per_day ? String(policy.requests_per_day) : 'uncapped', 'graph')
    ];
  }
}

class SessionsProvider {
  constructor(api) {
    this.api = api;
    this.sessions = [];
    this.lastError = '';
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  async refresh() {
    try {
      const data = await this.api.get('/sessions?limit=50');
      this.sessions = data.sessions || [];
      this.lastError = '';
    } catch (error) {
      this.sessions = [];
      this.lastError = error.message;
    }
    this.emitter.fire();
  }

  getTreeItem(item) {
    return item;
  }

  getChildren() {
    if (this.lastError) {
      return [new InfoItem('Sessions unavailable', this.lastError, 'warning')];
    }
    if (this.sessions.length === 0) {
      return [new InfoItem('No sessions', 'Start the CLI from VS Code', 'debug-start')];
    }
    return this.sessions.map((session) => new SessionItem(session));
  }
}

class InfoItem extends vscode.TreeItem {
  constructor(label, description, icon) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    this.iconPath = new vscode.ThemeIcon(icon);
  }
}

class SessionItem extends vscode.TreeItem {
  constructor(session) {
    super(`${session.user_id} · ${session.status}`, vscode.TreeItemCollapsibleState.None);
    this.session = session;
    this.description = `${session.role} · ${session.turn_count} turns`;
    this.tooltip = `${session.cwd}\n${session.model}\nLast seen: ${session.last_heartbeat_at}`;
    this.iconPath = new vscode.ThemeIcon(session.status === 'active' ? 'vm-active' : 'circle-outline');
    this.contextValue = session.status === 'active' ? 'activeSession' : 'session';
  }
}

function getNonce() {
  let text = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i += 1) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}

module.exports = {
  activate,
  deactivate
};
