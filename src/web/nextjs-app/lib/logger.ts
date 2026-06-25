/**
 * Structured logger utility for both server-side (Node.js) and client-side (browser) contexts.
 *
 * Server: ANSI-colored output matching the Python backend color scheme.
 * Browser: CSS-styled console output via %c format specifier.
 *
 * Color scheme (aligned with Python backend and globals.css palette):
 *   debug → Cyan    \x1b[36m  / color: #1a9ea6
 *   info  → Green   \x1b[32m  / color: #2a6b2a
 *   warn  → Yellow  \x1b[33m  / color: #b8860b
 *   error → Red     \x1b[31m  / color: #9b2335
 *
 * Log level controlled by NEXT_PUBLIC_LOG_LEVEL env var (default: "info").
 *
 * Usage:
 *   import { createLogger } from "@/lib/logger";
 *   const log = createLogger("api/health");
 *   log.info("Request received", { method: req.method });
 */

type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_PRIORITY: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

// ANSI codes for Node.js server-side output
const ANSI: Record<LogLevel, string> = {
  debug: "\x1b[36m", // Cyan
  info:  "\x1b[32m", // Green
  warn:  "\x1b[33m", // Yellow
  error: "\x1b[31m", // Red
};
const ANSI_RESET = "\x1b[0m";
const ANSI_DIM   = "\x1b[2m";
const ANSI_BOLD  = "\x1b[1m";

// CSS colors for browser devtools output
const BROWSER_CSS: Record<LogLevel, string> = {
  debug: "color: #1a9ea6; font-weight: normal",
  info:  "color: #2a6b2a; font-weight: normal",
  warn:  "color: #b8860b; font-weight: bold",
  error: "color: #9b2335; font-weight: bold",
};

const _currentLevel: LogLevel =
  (process.env.NEXT_PUBLIC_LOG_LEVEL as LogLevel | undefined) ?? "info";

function _shouldLog(level: LogLevel): boolean {
  return LEVEL_PRIORITY[level] >= LEVEL_PRIORITY[_currentLevel];
}

function _emit(level: LogLevel, module: string, msg: string, ...args: unknown[]): void {
  if (!_shouldLog(level)) return;

  const ts = new Date().toISOString().slice(11, 23); // HH:MM:SS.mmm
  const isServer = typeof window === "undefined";

  if (isServer) {
    const color = ANSI[level];
    const label = level.toUpperCase().padEnd(5);
    const line = `${ANSI_DIM}${ts}${ANSI_RESET} ${color}${ANSI_BOLD}${label}${ANSI_RESET} [${module}] ${color}${msg}${ANSI_RESET}`;
    // eslint-disable-next-line no-console
    (level === "debug" ? console.log : console[level])(line, ...args);
  } else {
    const label = level.toUpperCase().padEnd(5);
    // eslint-disable-next-line no-console
    (level === "debug" ? console.log : console[level])(
      `%c${ts}%c ${label}%c [${module}] ${msg}`,
      "color: gray; font-weight: normal",
      BROWSER_CSS[level],
      "color: inherit; font-weight: normal",
      ...args,
    );
  }
}

export interface Logger {
  debug: (msg: string, ...args: unknown[]) => void;
  info:  (msg: string, ...args: unknown[]) => void;
  warn:  (msg: string, ...args: unknown[]) => void;
  error: (msg: string, ...args: unknown[]) => void;
}

/** Create a logger bound to a module name (shown in log lines as [module]). */
export function createLogger(module: string): Logger {
  return {
    debug: (msg, ...args) => _emit("debug", module, msg, ...args),
    info:  (msg, ...args) => _emit("info",  module, msg, ...args),
    warn:  (msg, ...args) => _emit("warn",  module, msg, ...args),
    error: (msg, ...args) => _emit("error", module, msg, ...args),
  };
}
