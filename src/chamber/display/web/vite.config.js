import { defineConfig } from "vite";

// Desk stays a SINGLE self-contained file on purpose: `window.py:_app_html()`
// inlines it into an about:blank page, where a separate shared chunk
// (e.g. `./createLucideIcon.js`) cannot be imported. Keep this config
// single-input so Desk emits exactly desk.js + desk.css. The Console app
// builds separately via vite.console.config.js.
export default defineConfig({
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    assetsDir: "assets",
    rollupOptions: {
      output: {
        entryFileNames: "desk.js",
        chunkFileNames: "desk.js",
        assetFileNames: "desk.[ext]",
      },
    },
  },
});
