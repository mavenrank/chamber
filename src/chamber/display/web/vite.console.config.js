import { defineConfig } from "vite";

// Chamber Console app (Phase 1, read-only). Separate build from Desk so
// Desk keeps its single-file guarantee — see vite.config.js. Emits
// console.html + console.js + console.css into the same ../dist.
export default defineConfig({
  build: {
    outDir: "../dist",
    emptyOutDir: false,
    assetsDir: "assets",
    rollupOptions: {
      input: "console.html",
      output: {
        entryFileNames: "console.js",
        chunkFileNames: "console.[hash].js",
        assetFileNames: "console.[ext]",
      },
    },
  },
});
