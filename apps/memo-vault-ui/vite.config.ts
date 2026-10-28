import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { resolve } from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function localOwnerAutofill(dataDir: string | undefined): Plugin {
  return {
    name: "memo-vault-local-owner-autofill",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__memo_vault_local_owner", async (request, response) => {
        response.setHeader("Cache-Control", "no-store");
        const address = request.socket.remoteAddress;
        const host = request.headers.host;
        try {
          const url = new URL(`http://${host}`);
          const origin = request.headers.origin;
          if (
            request.method !== "GET" ||
            !["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(address ?? "") ||
            !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
            (origin && new URL(origin).host !== url.host) ||
            request.headers["sec-fetch-site"] === "cross-site"
          ) {
            response.statusCode = 403;
            response.end();
            return;
          }
          const configured = dataDir?.startsWith("~/")
            ? resolve(homedir(), dataDir.slice(2))
            : dataDir;
          const roots = configured
            ? [resolve(configured)]
            : [resolve(process.cwd(), "../../.memo-vault"), resolve(homedir(), ".memo-vault")];
          for (const root of roots) {
            try {
              const token = (await readFile(resolve(root, "owner-bootstrap.token"), "utf8")).trim();
              if (!token.startsWith("mvo_") || token.length < 40 || token.length > 256) continue;
              response.setHeader("Content-Type", "application/json");
              response.end(JSON.stringify({ token }));
              return;
            } catch {
              // The Vault may not have started yet, or it may use another data directory.
            }
          }
          response.statusCode = 404;
          response.end();
        } catch {
          response.statusCode = 403;
          response.end();
        }
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "MEMO_VAULT_");
  return {
    plugins: [react(), localOwnerAutofill(env.MEMO_VAULT_DATA_DIR)],
    server: {
      proxy: { "/api": env.MEMO_VAULT_UI_API_URL || "http://127.0.0.1:8765" },
    },
  };
});
