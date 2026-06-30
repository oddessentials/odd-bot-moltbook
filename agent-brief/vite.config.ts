import { jsxLocPlugin } from "@builder.io/vite-plugin-jsx-loc";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vite";
import { vitePluginManusRuntime } from "vite-plugin-manus-runtime";

export default defineConfig(({ command }) => {
  // `command` is "serve" for `vite dev` and "build" for `vite build`
  // (the daily/podcast pipelines run `vite build`).
  const isDev = command === "serve";

  // Manus scaffolder preview-editor tooling — DEV ONLY. In a production
  // build these are dead weight that ships to every visitor:
  //   - vitePluginManusRuntime injects a ~358 KB inline <script> (its own
  //     second copy of React + a parent-iframe postMessage bridge) into
  //     every prerendered page. It's uncacheable (inline), re-parsed on
  //     every navigation, and no-ops on the standalone GitHub Pages deploy
  //     where there is no Manus parent frame.
  //   - jsxLocPlugin stamps data-loc="file:line:col" on every JSX element
  //     for click-to-source in the Manus editor — useless in production.
  // The real `react()` plugin (above) does the actual JSX transform, so the
  // production app is fully functional without these. See issue #38.
  const plugins = [
    react(),
    tailwindcss(),
    ...(isDev ? [jsxLocPlugin(), vitePluginManusRuntime()] : []),
  ];

  return {
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
      rollupOptions: {
        output: {
          // Isolate the published brief corpus (data/briefs.json, ~273 KB
          // built) into its own chunk. That alone keeps the entry chunk
          // (~400 KB) under Vite's 500 KB warning threshold
          // WITHOUT splitting any JS module graph: React and every consumer
          // stay together in the one entry chunk, exactly as before, so there
          // is no cross-chunk React-init hazard (splitting react/radix apart
          // breaks `React.createContext` at module load). Data is a pure leaf
          // with no code deps, so isolating it is safe. The entry keeps Vite's
          // default `index-*.js` name, which src/post_x.py probes to verify
          // the deploy. Everything still loads eagerly (entry <script> +
          // modulepreload), so render/prerender/LCP behavior is unchanged.
          // See issue #38.
          manualChunks(id) {
            if (id.includes("/data/briefs.json")) return "data-briefs";
          },
        },
      },
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
  };
});
