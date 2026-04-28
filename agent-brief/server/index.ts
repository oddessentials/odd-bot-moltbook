import express from "express";
import fs from "fs/promises";
import { createServer } from "http";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Path to the engine's published-Briefs file. agent-brief lives inside
// the parent odd-bot-moltbook repo; data/briefs.json is two directories
// up from server/ in dev (or from dist/ in production).
const BRIEFS_PATH = path.resolve(__dirname, "..", "..", "data", "briefs.json");

async function startServer() {
  const app = express();
  const server = createServer(app);

  // /api/briefs — return the engine-produced published Briefs.
  // Empty array if the file doesn't exist yet (engine never ran).
  app.get("/api/briefs", async (_req, res) => {
    try {
      const text = await fs.readFile(BRIEFS_PATH, "utf-8");
      const briefs = JSON.parse(text);
      res.json(Array.isArray(briefs) ? briefs : []);
    } catch (err: any) {
      if (err && err.code === "ENOENT") {
        res.json([]);
        return;
      }
      console.error("[/api/briefs] failed:", err);
      res.status(500).json({ error: "failed to read briefs" });
    }
  });

  // Serve static files from dist/public in production
  const staticPath =
    process.env.NODE_ENV === "production"
      ? path.resolve(__dirname, "public")
      : path.resolve(__dirname, "..", "dist", "public");

  app.use(express.static(staticPath));

  // Handle client-side routing - serve index.html for all routes
  app.get("*", (_req, res) => {
    res.sendFile(path.join(staticPath, "index.html"));
  });

  const port = process.env.PORT || 3000;

  server.listen(port, () => {
    console.log(`Server running on http://localhost:${port}/`);
  });
}

startServer().catch(console.error);
