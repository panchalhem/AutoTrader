// Desktop shell: license-gates access, then spawns the existing Python
// dashboard.py (automation/) as a child process and displays it in a
// BrowserWindow with Basic Auth injected automatically (no password prompt
// for the person who already unlocked the app with a valid license).
//
// Dev-mode note: this reads the license server's public key straight out of
// ../license_server/keys/ (monorepo-relative) and finds the Python venv at
// ../.venv. A packaged build instead bundles the public key inside the app
// and ships its own embedded Python runtime — see the "Packaging" section
// of the README this ships with.

const { app, BrowserWindow, ipcMain, session } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');
const jwt = require('jsonwebtoken');

const REPO_ROOT = path.resolve(__dirname, '..');
const AUTOMATION_DIR = path.join(REPO_ROOT, 'automation');
// Packaged build: main.js ships inside app.asar, so __dirname-relative
// lookups above don't reach the unpacked repo. Everything the app needs at
// runtime instead comes from process.resourcesPath, populated by the
// electron-builder "extraResources" entries in package.json (see
// build-backend.sh/.ps1 which produce backend/win|mac/dashboard via
// PyInstaller before packaging).
const PUBLIC_KEY_FILE = app.isPackaged
  ? path.join(process.resourcesPath, 'license_public_key.pem')
  : path.join(REPO_ROOT, 'license_server', 'keys', 'license_public_key.pem');
// Dev mode writes/reads the password next to dashboard.py in the repo, same
// as running it from a terminal. Packaged mode instead points the backend at
// Electron's own userData dir via DASHBOARD_DATA_DIR (see spawnDashboard) —
// guaranteed writable and a real, stable path, unlike trying to guess where
// PyInstaller's frozen __file__ resolution lands inside the bundle.
const PACKAGED_DATA_DIR = path.join(app.getPath('userData'), 'dashboard-data');
const DASHBOARD_PASSWORD_FILE = app.isPackaged
  ? path.join(PACKAGED_DATA_DIR, '.dashboard_password')
  : path.join(AUTOMATION_DIR, 'data', '.dashboard_password');
const LICENSE_STORE_FILE = path.join(app.getPath('userData'), 'license.json');
const DASHBOARD_PORT = 8787;

let dashboardProcess = null;
let mainWindow = null;
let lockWindow = null;

function verifyLicenseToken(token) {
  if (!fs.existsSync(PUBLIC_KEY_FILE)) {
    return { valid: false, reason: 'no_public_key_bundled' };
  }
  const publicKey = fs.readFileSync(PUBLIC_KEY_FILE, 'utf8');
  try {
    const claims = jwt.verify(token, publicKey, { algorithms: ['RS256'] });
    return { valid: true, claims };
  } catch (err) {
    return { valid: false, reason: err.message };
  }
}

function loadStoredLicense() {
  if (!fs.existsSync(LICENSE_STORE_FILE)) return null;
  try {
    const { token } = JSON.parse(fs.readFileSync(LICENSE_STORE_FILE, 'utf8'));
    return token;
  } catch {
    return null;
  }
}

function saveLicense(token) {
  fs.mkdirSync(path.dirname(LICENSE_STORE_FILE), { recursive: true });
  fs.writeFileSync(LICENSE_STORE_FILE, JSON.stringify({ token }));
}

function waitForPort(port, timeoutMs = 20000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    (function attempt() {
      const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 1000 }, (res) => {
        res.destroy();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() - start > timeoutMs) return reject(new Error('dashboard did not start in time'));
        setTimeout(attempt, 500);
      });
      req.end();
    })();
  });
}

function spawnDashboard() {
  if (app.isPackaged) {
    // Frozen backend (PyInstaller --onedir): a self-contained dashboard.exe
    // (or "dashboard" on mac) bundled next to its _internal/ deps — no
    // system Python required on the end-user's machine.
    const exeName = process.platform === 'win32' ? 'dashboard.exe' : 'dashboard';
    const backendExe = path.join(process.resourcesPath, 'backend', exeName);
    fs.mkdirSync(PACKAGED_DATA_DIR, { recursive: true });
    dashboardProcess = spawn(backendExe, ['--no-open', '--port', String(DASHBOARD_PORT)], {
      cwd: path.dirname(backendExe),
      env: { ...process.env, DASHBOARD_DATA_DIR: PACKAGED_DATA_DIR },
    });
  } else {
    const venvPython = process.platform === 'win32'
      ? path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')
      : path.join(REPO_ROOT, '.venv', 'bin', 'python');
    const python = fs.existsSync(venvPython) ? venvPython : 'python3';

    dashboardProcess = spawn(python, ['dashboard.py', '--no-open', '--port', String(DASHBOARD_PORT)], {
      cwd: AUTOMATION_DIR,
      env: process.env,
    });
  }
  dashboardProcess.stdout.on('data', (d) => process.stdout.write(`[dashboard] ${d}`));
  dashboardProcess.stderr.on('data', (d) => process.stderr.write(`[dashboard] ${d}`));
  dashboardProcess.on('error', (err) => {
    process.stderr.write(`[dashboard] failed to start: ${err.message}\n`);
  });
}

async function openMainWindow() {
  spawnDashboard();
  await waitForPort(DASHBOARD_PORT);

  // Give dashboard.py a moment to have written its auto-generated password
  // file (happens at import time, before it starts serving) before reading it.
  for (let i = 0; i < 20 && !fs.existsSync(DASHBOARD_PASSWORD_FILE); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
  const password = fs.existsSync(DASHBOARD_PASSWORD_FILE)
    ? fs.readFileSync(DASHBOARD_PASSWORD_FILE, 'utf8').trim()
    : null;

  if (password) {
    const auth = 'Basic ' + Buffer.from(`admin:${password}`).toString('base64');
    session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
      details.requestHeaders['Authorization'] = auth;
      callback({ requestHeaders: details.requestHeaders });
    });
  }

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    title: 'Trading Automation',
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  mainWindow.loadURL(`http://127.0.0.1:${DASHBOARD_PORT}/`);
  mainWindow.on('closed', () => { mainWindow = null; });
}

function openLockWindow() {
  lockWindow = new BrowserWindow({
    width: 480,
    height: 360,
    title: 'Activate License',
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  lockWindow.loadFile(path.join(__dirname, 'lock.html'));
}

ipcMain.handle('activate-license', async (_event, token) => {
  const result = verifyLicenseToken(token);
  if (result.valid) {
    saveLicense(token);
    if (lockWindow) { lockWindow.close(); lockWindow = null; }
    await openMainWindow();
  }
  return result;
});

app.whenReady().then(() => {
  const storedToken = loadStoredLicense();
  if (storedToken) {
    const result = verifyLicenseToken(storedToken);
    if (result.valid) {
      openMainWindow();
      return;
    }
  }
  openLockWindow();
});

app.on('window-all-closed', () => {
  if (dashboardProcess) dashboardProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});
