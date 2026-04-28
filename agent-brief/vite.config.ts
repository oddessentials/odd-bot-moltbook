import { jsxLocPlugin } from "@builder.io/vite-plugin-jsx-loc";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vite";
import { vitePluginManusRuntime } from "vite-plugin-manus-runtime";

const plugins = [react(), tailwindcss(), jsxLocPlugin(), vitePluginManusRuntime()];

export default defineConfig({
  plugins,
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "client", "src"),
      "@shared": path.resolve(import.meta.dirname, "shared"),
      "@assets": path.resolve(import.meta.dirname, "attached_assets"),
    },
  },
  envDir: path.resolve(import.meta.dirname),
  root: path.resolve(import.meta.dirname, "client"),
  // Build target is repo-root /docs/ — GitHub Pages "main + /docs" source.
  // emptyOutDir wipes prior build artifacts; nothing else lives here.
  build: {
    outDir: path.resolve(import.meta.dirname, "..", "docs"),
    emptyOutDir: true,
  },
  server: {
    port: 3000,
    strictPort: false, // Will find next available port if 3000 is busy
    host: true,
    allowedHosts: [
      ".manuspre.computer",
      ".manus.computer",
      ".manus-asia.computer",
      ".manuscomputer.ai",
      ".manusvm.computer",
      "localhost",
      "127.0.0.1",
    ],
    fs: {
      strict: true,
      // Allow imports from `data/` so useBriefs can build-time-import
      // the engine's published briefs.json. The data dir lives outside
      // the Vite root (which is `client/`), so it must be explicitly
      // allowed.
      allow: [
        path.resolve(import.meta.dirname),
        path.resolve(import.meta.dirname, "..", "data"),
      ],
      deny: ["**/.*"],
    },
  },
});
