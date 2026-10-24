import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const GATEWAY = process.env.SLUICE_URL ?? "http://localhost:8000";
const REPO = resolve(__dirname, "..");

/** `virtual:git-log`: the repo's commits, read at build/dev time (the browser can't run git). */
function gitLog(): Plugin {
  const id = "virtual:git-log";
  return {
    name: "sluice-git-log",
    resolveId: (s) => (s === id ? "\0" + id : undefined),
    load(s) {
      if (s !== "\0" + id) return;
      let commits: unknown[] = [];
      try {
        const out = execFileSync(
          "git",
          ["log", "--reverse", "--date=short", "--pretty=format:%x1e%h%x1f%ad%x1f%s%x1f%b%x1f", "--shortstat"],
          { cwd: REPO, encoding: "utf8" },
        );
        commits = out
          .split("\x1e")
          .map((c) => c.trim())
          .filter(Boolean)
          .map((c) => {
            // a record: hash, date, subject, body, then git's --shortstat line
            const [hash, date, subject, body = "", rest = ""] = c.split("\x1f");
            const stat = /(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?/.exec(rest);
            return {
              hash, date, subject, body: body.trim(),
              files: stat ? Number(stat[1]) : 0,
              added: stat?.[2] ? Number(stat[2]) : 0,
              removed: stat?.[3] ? Number(stat[3]) : 0,
            };
          });
      } catch {
        // not a git checkout: the build log shows its empty state
      }
      return `export default ${JSON.stringify(commits)};`;
    },
  };
}

export default defineConfig({
  plugins: [react(), gitLog()],
  // the bundle inlines every ADR, note and bench result on purpose (~600 kB): a local tool
  build: { chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    fs: { allow: [REPO] }, // bench/results and docs/ are imported as raw text
    proxy: Object.fromEntries(
      ["/v1", "/mcp", "/metrics", "/healthz"].map((p) => [p, { target: GATEWAY, changeOrigin: true }]),
    ),
  },
});
